import logging
import tempfile
import os
import asyncio
import ssl
import random
from typing import Union
from functools import partial
from swpt_stomp.logging import configure_logging
from swpt_stomp.common import (
    WatermarkQueue, ServerError, Message, SSL_HANDSHAKE_TIMEOUT,
    SERVER_KEY, SERVER_CERT, NODEDATA_DIR, PROTOCOL_BROKER_URL,
    get_peer_serial_number,
)
from swpt_stomp.rmq import consume_from_queue
from swpt_stomp.peer_data import get_database_instance, NodeData, PeerData
from swpt_stomp.aio_protocols import StompClient

PROTOCOL_BROKER_QUEUE = os.environ.get('PROTOCOL_BROKER_QUEUE', 'default')
PEER_NODE_ID = os.environ.get('PEER_NODE_ID', 'UNKNOWN')
CLIENT_QUEUE_SIZE = int(os.environ.get('CLIENT_QUEUE_SIZE', '100'))
_logger = logging.getLogger(__name__)


async def connect(peer_node_id: str):
    db = get_database_instance(url=NODEDATA_DIR)
    owner_node_data = await db.get_node_data()
    peer_data = await db.get_peer_data(peer_node_id)
    if peer_data is None:
        raise RuntimeError(f'Peer {peer_node_id} is not in the database.')

    # To be correctly authenticated by the server, we must present both the
    # server certificate, and the sub-CA certificate issued by the peer's
    # root CA. Here we create a temporary file containing both certificates.
    # Note that this is a blocking operation, but this is OK, because we
    # will open no more than one client connection per process.
    with tempfile.NamedTemporaryFile() as certfile:
        with open(SERVER_CERT, 'br') as f:
            certfile.write(f.read())

        certfile.write(b'\n')
        certfile.write(peer_data.sub_cert)
        certfile.flush()

        # Configure SSL context:
        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ssl_context.verify_mode = ssl.CERT_REQUIRED
        ssl_context.check_hostname = False
        ssl_context.minimum_version = ssl.TLSVersion.TLSv1_3
        ssl_context.load_verify_locations(
            cadata=peer_data.root_cert.decode('ascii'))
        ssl_context.load_cert_chain(certfile=certfile.name, keyfile=SERVER_KEY)

        loop = asyncio.get_running_loop()
        server_host, server_port = random.choice(peer_data.servers)
        transport, protocol = await loop.create_connection(
            partial(_create_client_protocol, loop, owner_node_data, peer_data),
            host=server_host,
            port=server_port,
            ssl=ssl_context,
            ssl_handshake_timeout=SSL_HANDSHAKE_TIMEOUT,
        )
        await protocol.connection_lost_event.wait()


def _create_client_protocol(
        loop: asyncio.AbstractEventLoop,
        owner_node_data: NodeData,
        peer_data: PeerData,
) -> StompClient:
    send_queue: asyncio.Queue[Union[Message, None, ServerError]] = (
        asyncio.Queue(CLIENT_QUEUE_SIZE))
    recv_queue: WatermarkQueue[Union[str, None]] = (
        WatermarkQueue(CLIENT_QUEUE_SIZE))

    async def consume(transport: asyncio.Transport) -> None:
        try:
            peer_serial_number = get_peer_serial_number(transport)
            if peer_serial_number != peer_data.node_id:
                raise ServerError('Invalid certificate subject.')
        except ServerError as e:
            await send_queue.put(e)
        except (asyncio.CancelledError, Exception):
            await send_queue.put(ServerError('Abruptly closed connection.'))
            raise
        else:
            await consume_from_queue(
                send_queue,
                recv_queue,
                url=PROTOCOL_BROKER_URL,
                queue_name=PROTOCOL_BROKER_QUEUE,
                transform_message_body=partial(
                    _transform_message_body, owner_node_data, peer_data),
            )

    return StompClient(
        send_queue,
        recv_queue,
        start_message_processor=lambda t: loop.create_task(consume(t)),
        host=peer_data.stomp_host,
        send_destination=peer_data.stomp_destination,
    )


def _transform_message_body(
        owner_node_data: NodeData,
        peer_data: PeerData,
        message_body: bytes,
) -> bytearray:
    raise Exception


if __name__ == '__main__':
    configure_logging()
    asyncio.run(connect(PEER_NODE_ID), debug=True)
