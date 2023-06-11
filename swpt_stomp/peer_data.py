import re
import os
import os.path
import asyncio
from concurrent.futures import ThreadPoolExecutor
from abc import ABC, abstractmethod
from enum import Enum
from dataclasses import dataclass
from typing import NamedTuple, Optional

_DN_PART_RE = re.compile(r"(?!-)[a-z0-9-]{1,63}(?<!-)$", re.IGNORECASE)


class NodeType(Enum):
    AA = 1  # Accounting Authority
    CA = 2  # Creditors Agent
    DA = 3  # Debtors Agent


class Subnet(NamedTuple):
    subnet: int
    subnet_mask: int

    @classmethod
    def parse(cls, s):
        """Parse from a hexadecimal string."""
        n = 4 * len(s)
        if n > 64:
            raise ValueError(f'invalid subnet: {s}')

        try:
            subnet = (int(s, base=16) << (64 - n)) if n > 0 else 0
            if not 0 <= subnet < 0xffffffffffffffff:
                raise ValueError
        except ValueError:
            raise ValueError(f'invalid subnet: {s}')

        return Subnet(subnet, (-1 << (64 - n)) & 0xffffffffffffffff)


@dataclass
class NodeData:
    """Basic data about the owner of the node.
    """
    __slots__ = (
        'node_type',
        'node_id',
        'root_cert',
        'subnet',
    )
    node_type: NodeType
    node_id: str
    root_cert: bytes
    subnet: Optional[Subnet]


@dataclass
class PeerData:
    """Basic data about a peer of the owner of the node.
    """
    __slots__ = (
        'node_type',
        'node_id',
        'servers',
        'stomp_host',
        'stomp_destination',
        'root_cert',
        'peer_cert',
        'sub_cert',
        'subnet',
    )
    node_type: NodeType
    node_id: str
    servers: list[tuple[str, int]]
    stomp_host: Optional[str]
    stomp_destination: Optional[str]
    root_cert: bytes
    peer_cert: bytes
    sub_cert: Optional[bytes]
    subnet: Optional[Subnet]


class DatabaseError(Exception):
    """Thrown by `NodePeersDatabase` instances, to indicate an error."""


class NodePeersDatabase(ABC):
    """A database containing information for the node and its peers."""

    @abstractmethod
    async def get_node_data(self) -> NodeData:
        raise NotImplementedError  # pragma: nocover

    @abstractmethod
    async def get_peer_data(self, node_id: str) -> Optional[PeerData]:
        raise NotImplementedError  # pragma: nocover


def get_database_instance(url: str) -> NodePeersDatabase:
    """Return an instance of a node-info database.

    The location of the database is determined by the passed `url`
    parameter. Currently, only the "file://" scheme is supported for the
    URL, and it must refer to a local directory.

    For example:
    >>> db = get_database_instance('file:///path/to/the/database/')
    """
    if url.startswith('file:///'):
        return _LocalDirectory(url)

    raise ValueError(f'invalid database URL: {url}')


class _LocalDirectory(NodePeersDatabase):
    def __init__(self, url: str):
        assert url.startswith('file:///')
        self._root_dir: str = os.path.normpath(url[7:])
        self._loop = asyncio.get_event_loop()
        self._node_data: Optional[NodeData] = None
        n = str(os.cpu_count() or 1)
        self._executor = ThreadPoolExecutor(
            max_workers=int(os.environ.get('APP_EXECUTOR_THREADS', n)))

    def _read_file(self, filepath: str) -> bytes:
        abspath = os.path.join(self._root_dir, filepath)
        with open(abspath, 'br') as f:
            return f.read()

    async def read_file(self, filepath: str) -> bytes:
        return await self._loop.run_in_executor(
            self._executor,
            lambda: self._read_file(filepath),
        )

    async def read_pem_file(self, filepath: str) -> bytes:
        return await self.read_file(filepath)

    async def read_line(self, filepath: str) -> str:
        content = await self.read_file(filepath)
        if content.endswith(b'\r\n'):
            content = content[:-2]
        elif content.endswith(b'\n'):
            content = content[:-1]

        try:
            return content.decode('utf8')
        except UnicodeDecodeError as e:
            raise DatabaseError from e

    async def read_subnet_file(self, filepath: str) -> Optional[Subnet]:
        try:
            s = await self.read_line(filepath)
        except FileNotFoundError:
            return None

        try:
            return Subnet.parse(s)
        except ValueError as e:
            raise DatabaseError from e

    async def get_node_data(self) -> NodeData:
        if self._node_data is None:
            root_cert = await self.read_file('root-ca.crt')
            node_id = await self.read_line('db/nodeid')
            node_type_str = await self.read_line('db/nodetype')
            try:
                node_type = _parse_node_type(node_type_str)
            except ValueError as e:
                raise DatabaseError from e

            subnet = None
            if node_type == NodeType.CA:
                subnet = await self.read_subnet_file('creditors-subnet.txt')
            elif node_type == NodeType.DA:
                subnet = await self.read_subnet_file('debtors-subnet.txt')

            self._node_data = NodeData(
                node_type=node_type,
                node_id=node_id,
                root_cert=root_cert,
                subnet=subnet,
            )

        return self._node_data

    async def get_peer_data(self, node_id: str) -> Optional[PeerData]:
        raise NotImplementedError


def _parse_node_type(s: str) -> NodeType:
    if s == "Accounting Authorities":
        return NodeType.AA
    elif s == "Creditors Agents":
        return NodeType.CA
    elif s == "Debtors Agents":
        return NodeType.DA
    else:
        raise ValueError(f'invalid node type: {s}')


def _parse_servers(s: str) -> list[tuple[str, int]]:
    servers = []
    for server in s.split(maxsplit=10000):
        try:
            host, port_str = server.split(':', maxsplit=1)
        except ValueError:
            raise ValueError(f'invalid server: {s}')

        if not _is_valid_hostname(host):
            raise ValueError(f'invalid host: {host}')

        try:
            port = int(port_str)
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            raise ValueError(f'invalid port: {port_str}')

        servers.append((host, port))

    return servers


def _is_valid_hostname(hostname):
    if hostname[-1] == ".":
        # strip exactly one dot from the right, if present
        hostname = hostname[:-1]

    if len(hostname) > 253:
        return False

    labels = hostname.split(".")
    return all(_DN_PART_RE.match(label) for label in labels)
