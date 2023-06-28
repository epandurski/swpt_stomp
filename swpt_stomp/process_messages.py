from typing import Union, Any
from swpt_stomp.common import Message, ServerError
from swpt_stomp.peer_data import NodeData, PeerData, NodeType, Subnet
from swpt_stomp.rmq import RmqMessage, HeadersType
from swpt_stomp.smp_schemas import (
    JSON_SCHEMAS, CLIENT_MESSAGE_TYPES, SERVER_MESSAGE_TYPES, ValidationError,
)


class ProcessingError(Exception):
    """Indicates that the message can not be processed.
    """
    def __init__(self, error_message: str):
        super().__init__(error_message)
        self.error_message = error_message


def transform_message(
        owner_node_data: NodeData,
        peer_data: PeerData,
        message: RmqMessage,
) -> Message:
    owner_node_type = owner_node_data.node_type
    msg_data = _parse_message_body(
        message,
        allow_client_messages=(owner_node_type != NodeType.AA),
        allow_server_messages=(owner_node_type == NodeType.AA),
    )

    creditor_id: int = msg_data['creditor_id']
    if not owner_node_data.creditors_subnet.match(creditor_id):
        raise ProcessingError(
            f'Invalid creditor ID: {_as_hex(creditor_id)}.')

    debtor_id: int = msg_data['debtor_id']
    if not peer_data.debtors_subnet.match(debtor_id):
        raise ProcessingError(
            f'Invalid debtor ID: {_as_hex(debtor_id)}.')

    if owner_node_type == NodeType.CA:
        msg_data['creditor_id'] = _change_subnet(
            creditor_id,
            from_=owner_node_data.creditors_subnet,
            to_=peer_data.creditors_subnet,
        )

    msg_json = JSON_SCHEMAS[message.type].dumps(msg_data)
    return Message(
        id=message.id,
        type=message.type,
        body=bytearray(msg_json.encode('utf8')),
        content_type='application/json',
    )


async def preprocess_message(
        owner_node_data: NodeData,
        peer_data: PeerData,
        message: Message,
) -> RmqMessage:
    try:
        owner_node_type = owner_node_data.node_type
        msg_data = _parse_message_body(
            message,
            allow_client_messages=(owner_node_type == NodeType.AA),
            allow_server_messages=(owner_node_type != NodeType.AA),
        )

        creditor_id: int = msg_data['creditor_id']
        if not peer_data.creditors_subnet.match(creditor_id):
            raise ProcessingError(
                f'Invalid creditor ID: {_as_hex(creditor_id)}.')

        debtor_id: int = msg_data['debtor_id']
        if not peer_data.debtors_subnet.match(debtor_id):
            raise ProcessingError(
                f'Invalid debtor ID: {_as_hex(debtor_id)}.')

        if owner_node_type == NodeType.CA:
            msg_data['creditor_id'] = _change_subnet(
                creditor_id,
                from_=peer_data.creditors_subnet,
                to_=owner_node_data.creditors_subnet,
            )

        msg_type = message.type
        headers: HeadersType = {
            'message-type': msg_type,
            'debtor-id': debtor_id,
            'creditor-id': creditor_id,
        }
        if 'coordinator_id' in msg_data:
            headers['coordinator-id'] = msg_data['coordinator_id']
            headers['coordinator-type'] = msg_data['coordinator_type']
            # TODO: Verify "coordinator-type".

        msg_json = JSON_SCHEMAS[msg_type].dumps(msg_data)
        return RmqMessage(
            id=message.id,
            body=msg_json.encode('utf8'),
            headers=headers,
            type=msg_type,
            content_type='application/json',
            routing_key='',  # TODO: Set a routing key.
        )

    except ProcessingError as e:
        raise ServerError(
            error_message=e.error_message,
            receipt_id=message.id,
            context=message.body,
            context_type=message.type,
            context_content_type=message.content_type,
        )


def _parse_message_body(
        m: Union[Message, RmqMessage],
        *,
        allow_client_messages: bool = True,
        allow_server_messages: bool = True,
) -> Any:
    if m.content_type != 'application/json':
        raise ProcessingError(f'Unsupported content type: {m.content_type}.')

    msg_type = m.type
    try:
        if not allow_client_messages and msg_type in CLIENT_MESSAGE_TYPES:
            raise KeyError
        if not allow_server_messages and msg_type in SERVER_MESSAGE_TYPES:
            raise KeyError
        schema = JSON_SCHEMAS[msg_type]
    except KeyError:
        raise ProcessingError(f'Invalid message type: {msg_type}.')

    try:
        body = m.body.decode('utf8')
    except UnicodeDecodeError:
        raise ProcessingError('UTF-8 decode error.')

    try:
        return schema.loads(body)
    except ValidationError as e:
        raise ProcessingError(f'Invalid {msg_type} message.') from e


def _change_subnet(creditor_id, *, from_: Subnet, to_: Subnet) -> int:
    """Translate `creditor_id` from one subnet to another.
    """
    mask = from_.subnet_mask
    assert mask == to_.subnet_mask
    subnet = creditor_id & mask
    assert subnet == from_.subnet
    relative_id = subnet ^ creditor_id
    return to_.subnet | relative_id


def _as_hex(n: int) -> str:
    return '0x' + n.to_bytes(8, byteorder='big', signed=True).hex()