# Parsec Cloud (https://parsec.cloud) Copyright (c) AGPLv3 2019 Scille SAS

import pytest


@pytest.mark.win32
def test_rename_to_another_drive(mountpoint_service):
    async def _bootstrap(fs, mountpoint_manager):
        await fs.workspace_create("/x")
        await fs.workspace_create("/y")
        await fs.file_create("/x/foo.txt")
        await mountpoint_manager.mount_all()

    mountpoint_service.start()
    mountpoint_service.execute(_bootstrap)
    x_path = mountpoint_service.get_workspace_mountpoint("x")
    y_path = mountpoint_service.get_workspace_mountpoint("y")

    with pytest.raises(OSError) as exc:
        (x_path / "foo.txt").rename(y_path)
    assert str(exc.value).startswith(
        "[WinError 17] The system cannot move the file to a different disk drive"
    )