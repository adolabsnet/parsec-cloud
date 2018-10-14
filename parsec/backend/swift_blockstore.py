from unittest.mock import Mock
import pbr.version

from parsec.backend.blockstore import BaseBlockStoreComponent
from parsec.backend.exceptions import AlreadyExistsError, NotFoundError

original_version_info = pbr.version.VersionInfo


def side_effect(key):
    if key == "python-swiftclient":
        return "3.5.0"

    else:
        return original_version_info(key)


pbr.version.VersionInfo = Mock(side_effect=side_effect)

import swiftclient  # noqa
from swiftclient.exceptions import ClientException  # noqa


class SwiftBlockStoreComponent(BaseBlockStoreComponent):
    def __init__(self, auth_url, tenant, container, user, password):
        self.swift_client = swiftclient.Connection(
            authurl=auth_url, user=":".join([user, tenant]), key=password
        )
        self._container = container
        self.swift_client.head_container(container)

    async def get(self, id):
        try:
            _, obj = self.swift_client.get_object(self._container, str(id))
        except ClientException as exc:
            if exc.http_status == 404:
                raise NotFoundError("Unknown block id.")

            else:
                raise exc

        return obj

    async def post(self, id, block):
        # TODO find a more efficient way to check if block already exists
        try:
            _, obj = self.swift_client.get_object(self._container, str(id))
        except ClientException as exc:
            if exc.http_status == 404:
                self.swift_client.put_object(self._container, str(id), block)
            else:
                raise exc

        else:
            raise AlreadyExistsError("A block already exists with id `%s`." % id)

        return id
