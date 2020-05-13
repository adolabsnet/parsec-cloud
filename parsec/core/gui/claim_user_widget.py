# Parsec Cloud (https://parsec.cloud) Copyright (c) AGPLv3 2019 Scille SAS

from PyQt5.QtCore import pyqtSignal
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import QWidget, QDialog

from enum import IntEnum

import trio

from structlog import get_logger

from parsec.api.protocol import DeviceID, HumanHandle
from parsec.core.types import LocalDevice
from parsec.core.local_device import LocalDeviceAlreadyExistsError, save_device_with_password

from parsec.core.invite import claimer_retrieve_info
from parsec.core.backend_connection import backend_invited_cmds_factory, BackendConnectionRefused

from parsec.core.gui import validators
from parsec.core.gui.trio_thread import JobResultError, ThreadSafeQtSignal
from parsec.core.gui.desktop import get_default_device
from parsec.core.gui.custom_dialogs import show_error, GreyedDialog, show_info
from parsec.core.gui.lang import translate as _
from parsec.core.gui.password_validation import get_password_strength
from parsec.core.gui.ui.claim_user_widget import Ui_ClaimUserWidget
from parsec.core.gui.ui.claim_user_code_exchange_widget import Ui_ClaimUserCodeExchangeWidget
from parsec.core.gui.ui.claim_user_provide_info_widget import Ui_ClaimUserProvideInfoWidget
from parsec.core.gui.ui.claim_user_instructions_widget import Ui_ClaimUserInstructionsWidget
from parsec.core.gui.ui.claim_user_finalize_widget import Ui_ClaimUserFinalizeWidget

logger = get_logger()


class Claimer:
    class Step(IntEnum):
        RetrieveInfo = 1
        WaitPeer = 2
        GetGreeterSas = 3
        SignifyTrust = 4
        GetClaimerSas = 5
        WaitPeerTrust = 6
        ClaimUser = 7

    def __init__(self):
        self.main_oob_send, self.main_oob_recv = trio.open_memory_channel(0)
        self.job_oob_send, self.job_oob_recv = trio.open_memory_channel(0)

    async def run(self, addr):
        try:
            async with backend_invited_cmds_factory(addr=addr) as cmds:
                r = await self.main_oob_recv.receive()

                assert r == self.Step.RetrieveInfo
                try:
                    initial_ctx = await claimer_retrieve_info(cmds)
                    await self.job_oob_send.send((True, None, initial_ctx.claimer_email))
                except Exception as exc:
                    await self.job_oob_send.send((False, exc, None))

                r = await self.main_oob_recv.receive()

                assert r == self.Step.WaitPeer
                try:
                    in_progress_ctx = await initial_ctx.do_wait_peer()
                    await self.job_oob_send.send((True, None))
                except Exception as exc:
                    await self.job_oob_send.send((False, exc))

                r = await self.main_oob_recv.receive()

                assert r == self.Step.GetGreeterSas
                try:
                    choices = in_progress_ctx.generate_greeter_sas_choices(size=4)
                    await self.job_oob_send.send((True, None, in_progress_ctx.greeter_sas, choices))
                except Exception as exc:
                    await self.job_oob_send.send((False, exc, None, None))

                r = await self.main_oob_recv.receive()

                assert r == self.Step.SignifyTrust
                try:
                    in_progress_ctx = await in_progress_ctx.do_signify_trust()
                    await self.job_oob_send.send((True, None))
                except Exception as exc:
                    await self.job_oob_send.send((False, exc))

                r = await self.main_oob_recv.receive()

                assert r == self.Step.GetClaimerSas
                await self.job_oob_send.send(in_progress_ctx.claimer_sas)

                r = await self.main_oob_recv.receive()

                assert r == self.Step.WaitPeerTrust
                try:
                    in_progress_ctx = await in_progress_ctx.do_wait_peer_trust()
                    await self.job_oob_send.send((True, None))
                except Exception as exc:
                    await self.job_oob_send.send((False, exc))

                r = await self.main_oob_recv.receive()
                assert r == self.Step.ClaimUser

                try:
                    device_id, human_handle = await self.main_oob_recv.receive()

                    new_device = await in_progress_ctx.do_claim_user(
                        requested_device_id=device_id, requested_human_handle=human_handle
                    )
                    await self.job_oob_send.send((True, None, new_device))
                except Exception as exc:
                    await self.job_oob_send.send((False, exc, None))
        except BackendConnectionRefused as exc:
            raise JobResultError(status="invitation-not-found", origin=exc)

    async def retrieve_info(self):
        await self.main_oob_send.send(self.Step.RetrieveInfo)
        r, exc, user_email = await self.job_oob_recv.receive()
        if not r:
            raise JobResultError(status="retrieve-info-failed", origin=exc)
        return user_email

    async def wait_peer(self):
        await self.main_oob_send.send(self.Step.WaitPeer)
        r, exc = await self.job_oob_recv.receive()
        if not r:
            raise JobResultError(status="wait-peer-failed", origin=exc)

    async def get_greeter_sas(self):
        await self.main_oob_send.send(self.Step.GetGreeterSas)
        r, exc, greeter_sas, choices = await self.job_oob_recv.receive()
        if not r:
            raise JobResultError(status="get-greeter-sas-failed", origin=exc)
        return greeter_sas, choices

    async def signify_trust(self):
        await self.main_oob_send.send(self.Step.SignifyTrust)
        r, exc = await self.job_oob_recv.receive()
        if not r:
            raise JobResultError(status="signify-trust-failed", origin=exc)

    async def get_claimer_sas(self):
        await self.main_oob_send.send(self.Step.GetClaimerSas)
        claimer_sas = await self.job_oob_recv.receive()
        return claimer_sas

    async def wait_peer_trust(self):
        await self.main_oob_send.send(self.Step.WaitPeerTrust)
        r, exc = await self.job_oob_recv.receive()
        if not r:
            raise JobResultError(status="wait-trust-failed", origin=exc)

    async def claim_user(self, device_id, human_handle):
        await self.main_oob_send.send(self.Step.ClaimUser)
        await self.main_oob_send.send((device_id, human_handle))
        r, exc, new_device = await self.job_oob_recv.receive()
        if not r:
            raise JobResultError(status="claim-user-failed", origin=exc)
        return new_device


class ClaimUserFinalizeWidget(QWidget, Ui_ClaimUserFinalizeWidget):
    succeeded = pyqtSignal(LocalDevice, str)
    failed = pyqtSignal()

    def __init__(self, config, new_device):
        super().__init__()
        self.setupUi(self)
        self.config = config
        self.new_device = new_device
        self.line_edit_password.textChanged.connect(
            self.password_strength_widget.on_password_change
        )
        self.line_edit_password.textChanged.connect(self.check_infos)
        self.line_edit_password_check.textChanged.connect(self.check_infos)
        self.button_finalize.clicked.connect(self._on_finalize_clicked)

    def check_infos(self, _=""):
        if (
            len(self.line_edit_password.text())
            and get_password_strength(self.line_edit_password.text()) > 0
            and self.line_edit_password.text() == self.line_edit_password_check.text()
        ):
            self.button_finalize.setDisabled(False)
        else:
            self.button_finalize.setDisabled(True)

    def _on_finalize_clicked(self):
        password = self.line_edit_password.text()
        try:
            save_device_with_password(self.config.config_dir, self.new_device, password)
            self.succeeded.emit(self.new_device, password)
        except LocalDeviceAlreadyExistsError as exc:
            show_error(self, _("TEXT_CLAIM_USER_DEVICE_ALREADY_EXISTS"), exception=exc)
            self.failed.emit()


class ClaimUserCodeExchangeWidget(QWidget, Ui_ClaimUserCodeExchangeWidget):
    succeeded = pyqtSignal()
    failed = pyqtSignal()

    signify_trust_success = pyqtSignal()
    signify_trust_error = pyqtSignal()

    wait_peer_trust_success = pyqtSignal()
    wait_peer_trust_error = pyqtSignal()

    get_greeter_sas_success = pyqtSignal()
    get_greeter_sas_error = pyqtSignal()

    get_claimer_sas_success = pyqtSignal()
    get_claimer_sas_error = pyqtSignal()

    def __init__(self, jobs_ctx, claimer):
        super().__init__()
        self.setupUi(self)
        self.claimer = claimer
        self.jobs_ctx = jobs_ctx

        self.signify_trust_job = None
        self.wait_peer_trust_job = None
        self.get_greeter_sas_job = None
        self.get_claimer_sas_job = None

        self.widget_claimer_code.hide()

        font = self.line_edit_claimer_code.font()
        font.setBold(True)
        font.setLetterSpacing(QFont.PercentageSpacing, 180)
        self.line_edit_claimer_code.setFont(font)

        self.code_input_widget.good_code_clicked.connect(self._on_good_greeter_code_clicked)
        self.code_input_widget.wrong_code_clicked.connect(self._on_wrong_greeter_code_clicked)
        self.code_input_widget.none_clicked.connect(self._on_none_clicked)

        self.get_greeter_sas_success.connect(self._on_get_greeter_sas_success)
        self.get_greeter_sas_error.connect(self._on_get_greeter_sas_error)
        self.get_claimer_sas_success.connect(self._on_get_claimer_sas_success)
        self.get_claimer_sas_error.connect(self._on_get_claimer_sas_error)
        self.signify_trust_success.connect(self._on_signify_trust_success)
        self.signify_trust_error.connect(self._on_signify_trust_error)
        self.wait_peer_trust_success.connect(self._on_wait_peer_trust_success)
        self.wait_peer_trust_error.connect(self._on_wait_peer_trust_error)

        self.get_greeter_sas_job = self.jobs_ctx.submit_job(
            ThreadSafeQtSignal(self, "get_greeter_sas_success"),
            ThreadSafeQtSignal(self, "get_greeter_sas_error"),
            self.claimer.get_greeter_sas,
        )

    def _on_good_greeter_code_clicked(self):
        self.widget_greeter_code.setDisabled(True)
        self.signify_trust_job = self.jobs_ctx.submit_job(
            ThreadSafeQtSignal(self, "signify_trust_success"),
            ThreadSafeQtSignal(self, "signify_trust_error"),
            self.claimer.signify_trust,
        )

    def _on_wrong_greeter_code_clicked(self):
        show_error(self, _("TEXT_CLAIM_USER_INVALID_CODE_CLICKED"))
        self.failed.emit()

    def _on_none_clicked(self):
        show_info(self, _("TEXT_CLAIM_USER_NONE_CODE_CLICKED"))
        self.failed.emit()

    def _on_get_greeter_sas_success(self):
        assert self.get_greeter_sas_job
        assert self.get_greeter_sas_job.is_finished()
        assert self.get_greeter_sas_job.status == "ok"
        greeter_sas, choices = self.get_greeter_sas_job.ret
        self.code_input_widget.set_choices(choices, greeter_sas)
        self.get_greeter_sas_job = None

    def _on_get_greeter_sas_error(self):
        assert self.get_greeter_sas_job
        assert self.get_greeter_sas_job.is_finished()
        assert self.get_greeter_sas_job.status != "ok"
        if self.get_greeter_sas_job.status != "cancelled":
            exc = None
            if self.get_greeter_sas_job.exc:
                exc = self.get_greeter_sas_job.exc.params.get("origin", None)
            show_error(self, _("TEXT_CLAIM_USER_GET_GREETER_SAS_ERROR"), exception=exc)
        self.get_greeter_sas_job = None
        self.failed.emit()

    def _on_get_claimer_sas_success(self):
        assert self.get_claimer_sas_job
        assert self.get_claimer_sas_job.is_finished()
        assert self.get_claimer_sas_job.status == "ok"
        claimer_sas = self.get_claimer_sas_job.ret
        self.get_claimer_sas_job = None
        self.widget_greeter_code.setVisible(False)
        self.widget_claimer_code.setVisible(True)
        self.line_edit_claimer_code.setText(str(claimer_sas))
        self.wait_peer_trust_job = self.jobs_ctx.submit_job(
            ThreadSafeQtSignal(self, "wait_peer_trust_success"),
            ThreadSafeQtSignal(self, "wait_peer_trust_error"),
            self.claimer.wait_peer_trust,
        )

    def _on_get_claimer_sas_error(self):
        assert self.get_claimer_sas_job
        assert self.get_claimer_sas_job.is_finished()
        assert self.get_claimer_sas_job.status != "ok"
        self.get_claimer_sas_job = None
        self.failed.emit()

    def _on_signify_trust_success(self):
        assert self.signify_trust_job
        assert self.signify_trust_job.is_finished()
        assert self.signify_trust_job.status == "ok"
        self.signify_trust_job = None
        self.get_claimer_sas_job = self.jobs_ctx.submit_job(
            ThreadSafeQtSignal(self, "get_claimer_sas_success"),
            ThreadSafeQtSignal(self, "get_claimer_sas_error"),
            self.claimer.get_claimer_sas,
        )

    def _on_signify_trust_error(self):
        assert self.signify_trust_job
        assert self.signify_trust_job.is_finished()
        assert self.signify_trust_job.status != "ok"
        if self.signify_trust_job.status != "cancelled":
            exc = None
            if self.signify_trust_job.exc:
                exc = self.signify_trust_job.exc.params.get("origin", None)
            show_error(self, _("TEXT_CLAIM_USER_WAIT_TRUST_ERROR"), exception=exc)
        self.signify_trust_job = None
        self.failed.emit()

    def _on_wait_peer_trust_success(self):
        assert self.wait_peer_trust_job
        assert self.wait_peer_trust_job.is_finished()
        assert self.wait_peer_trust_job.status == "ok"
        self.wait_peer_trust_job = None
        self.succeeded.emit()

    def _on_wait_peer_trust_error(self):
        assert self.wait_peer_trust_job
        assert self.wait_peer_trust_job.is_finished()
        assert self.wait_peer_trust_job.status != "ok"
        if self.wait_peer_trust_job.status != "cancelled":
            exc = None
            if self.wait_peer_trust_job.exc:
                exc = self.wait_peer_trust_job.exc.params.get("origin", None)
            show_error(self, _("TEXT_CLAIM_USER_WAIT_PEER_TRUST_ERROR"), exception=exc)
        self.wait_peer_trust_job = None
        self.failed.emit()

    def cancel(self):
        if self.signify_trust_job:
            self.signify_trust_job.cancel_and_join()
        if self.wait_peer_trust_job:
            self.wait_peer_trust_job.cancel_and_join()
        if self.get_claimer_sas_job:
            self.get_claimer_sas_job.cancel_and_join()
        if self.get_greeter_sas_job:
            self.get_greeter_sas_job.cancel_and_join()


class ClaimUserProvideInfoWidget(QWidget, Ui_ClaimUserProvideInfoWidget):
    succeeded = pyqtSignal()
    failed = pyqtSignal()

    claim_success = pyqtSignal()
    claim_error = pyqtSignal()

    def __init__(self, jobs_ctx, claimer, user_email):
        super().__init__()
        self.setupUi(self)
        self.jobs_ctx = jobs_ctx
        self.claimer = claimer
        self.claim_job = None
        self.new_device = None
        self.line_edit_user_full_name.setFocus()
        self.line_edit_user_email.setText(user_email)
        self.line_edit_device.setText(get_default_device())
        self.line_edit_user_full_name.textChanged.connect(self.check_infos)
        self.line_edit_device.textChanged.connect(self.check_infos)
        self.line_edit_device.setValidator(validators.DeviceNameValidator())
        self.claim_success.connect(self._on_claim_success)
        self.claim_error.connect(self._on_claim_error)
        self.label_wait.hide()
        self.button_ok.clicked.connect(self._on_claim_clicked)

    def check_infos(self, _=""):
        if self.line_edit_user_full_name.text() and self.line_edit_device.text():
            self.button_ok.setDisabled(False)
        else:
            self.button_ok.setDisabled(True)

    def _on_claim_clicked(self):
        device_id = None
        human_handle = None
        try:
            device_id = DeviceID("{}@{}".format(self.user_id, self.line_edit_device.text()))
        except ValueError as exc:
            show_error(self, _("TEXT_CLAIM_USER_INVALID_DEVICE_ID"), exception=exc)
            return
        try:
            human_handle = HumanHandle(
                email=self.line_edit_user_email.text(), label=self.line_edit_user_full_name.text()
            )
        except ValueError as exc:
            show_error(self, _("TEXT_CLAIM_USER_INVALID_HUMAN_HANDLE"), exception=exc)
            return
        self.button_ok.setDisabled(True)
        self.widget_info.setDisabled(True)
        self.label_wait.show()
        self.claim_job = self.jobs_ctx.submit_job(
            ThreadSafeQtSignal(self, "claim_success"),
            ThreadSafeQtSignal(self, "claim_error"),
            self.claimer.claim_user,
            device_id=device_id,
            human_handle=human_handle,
        )

    def _on_claim_success(self):
        assert self.claim_job
        assert self.claim_job.is_finished()
        assert self.claim_job.status == "ok"
        self.new_device = self.claim_job.ret
        self.claim_job = None
        self.succeeded.emit()

    def _on_claim_error(self):
        assert self.claim_job
        assert self.claim_job.is_finished()
        assert self.claim_job.status != "ok"
        if self.claim_job.status != "cancelled":
            exc = None
            if self.claim_job.exc:
                exc = self.claim_job.exc.params.get("origin", None)
            show_error(self, _("TEXT_CLAIM_USER_CLAIM_ERROR"), exception=exc)
        self.claim_job = None
        self.check_infos()
        self.widget_info.setDisabled(False)
        self.label_wait.hide()
        self.failed.emit()

    def cancel(self):
        if self.claim_job:
            self.claim_job.cancel_and_join()


class ClaimUserInstructionsWidget(QWidget, Ui_ClaimUserInstructionsWidget):
    succeeded = pyqtSignal()
    failed = pyqtSignal()

    wait_peer_success = pyqtSignal()
    wait_peer_error = pyqtSignal()

    def __init__(self, jobs_ctx, claimer, user_email):
        super().__init__()
        self.setupUi(self)
        self.jobs_ctx = jobs_ctx
        self.claimer = claimer
        self.wait_peer_job = None
        self.button_start.clicked.connect(self._on_button_start_clicked)
        self.wait_peer_success.connect(self._on_wait_peer_success)
        self.wait_peer_error.connect(self._on_wait_peer_error)

    def _on_button_start_clicked(self):
        self.button_start.setDisabled(True)
        self.button_start.setText("TEXT_CLAIM_USER_WAITING")
        self.wait_peer_job = self.jobs_ctx.submit_job(
            ThreadSafeQtSignal(self, "wait_peer_success"),
            ThreadSafeQtSignal(self, "wait_peer_error"),
            self.claimer.wait_peer,
        )

    def _on_wait_peer_success(self):
        assert self.wait_peer_job
        assert self.wait_peer_job.is_finished()
        assert self.wait_peer_job.status == "ok"
        self.wait_peer_job = None
        self.succeeded.emit()

    def _on_wait_peer_error(self):
        assert self.wait_peer_job
        assert self.wait_peer_job.is_finished()
        assert self.wait_peer_job.status != "ok"
        if self.wait_peer_job.status != "cancelled":
            exc = None
            if self.wait_peer_job.exc:
                exc = self.wait_peer_job.exc.params.get("origin", None)
            self.button_start.setDisabled(False)
            self.button_start.setText("ACTION_START")
            show_error(self, "TEXT_CLAIM_USER_WAIT_PEER_ERROR", exception=exc)
        self.wait_peer_job = None

    def cancel(self):
        if self.wait_peer_job:
            self.wait_peer_job.cancel_and_join()


class ClaimUserWidget(QWidget, Ui_ClaimUserWidget):
    claimer_success = pyqtSignal()
    claimer_error = pyqtSignal()
    retrieve_info_success = pyqtSignal()
    retrieve_info_error = pyqtSignal()

    def __init__(self, jobs_ctx, config, addr):
        super().__init__()
        self.setupUi(self)
        self.jobs_ctx = jobs_ctx
        self.config = config
        self.dialog = None
        self.addr = addr
        self.status = None
        self.user_email = None
        self.claimer_job = None
        self.retrieve_info_job = None
        self.claimer_success.connect(self._on_claimer_success)
        self.claimer_error.connect(self._on_claimer_error)
        self.retrieve_info_success.connect(self._on_retrieve_info_success)
        self.retrieve_info_error.connect(self._on_retrieve_info_error)
        self.claimer = Claimer()
        self._run_claimer()

    def _run_claimer(self):
        self.claimer_job = self.jobs_ctx.submit_job(
            ThreadSafeQtSignal(self, "claimer_success"),
            ThreadSafeQtSignal(self, "claimer_error"),
            self.claimer.run,
            addr=self.addr,
        )
        self.retrieve_info_job = self.jobs_ctx.submit_job(
            ThreadSafeQtSignal(self, "retrieve_info_success"),
            ThreadSafeQtSignal(self, "retrieve_info_error"),
            self.claimer.retrieve_info,
        )

    def _on_retrieve_info_success(self):
        assert self.retrieve_info_job
        assert self.retrieve_info_job.is_finished()
        assert self.retrieve_info_job.status == "ok"
        self.user_email = self.retrieve_info_job.ret
        self.retrieve_info_job = None
        self._goto_page1()

    def _on_retrieve_info_error(self):
        assert self.retrieve_info_job
        assert self.retrieve_info_job.is_finished()
        assert self.retrieve_info_job.status != "ok"
        if self.retrieve_info_job.status != "cancelled":
            exc = None
            if self.retrieve_info_job.exc:
                exc = self.retrieve_info_job.exc.params.get("origin", None)
                show_error(self, _("TEXT_CLAIM_USER_FAILED_TO_RETRIEVE_INFO"), exception=exc)
                self._on_page_failure_stop()
        self.retrieve_info_job = None

    def _on_page_failure_stop(self):
        self.dialog.accept()

    def _on_page_failure_reboot(self):
        self.restart()

    def restart(self):
        self.cancel()
        self.status = None
        self.claimer = Claimer()
        self._run_claimer()

    def _goto_page1(self):
        item = self.main_layout.takeAt(0)
        if item:
            current_page = item.widget()
            if current_page:
                current_page.hide()
                current_page.setParent(None)
        page = ClaimUserInstructionsWidget(self.jobs_ctx, self.claimer, self.user_email)
        page.succeeded.connect(self._goto_page2)
        page.failed.connect(self._on_page_failure_reboot)
        self.main_layout.insertWidget(0, page)

    def _goto_page2(self):
        current_page = self.main_layout.takeAt(0).widget()
        current_page.hide()
        current_page.setParent(None)
        page = ClaimUserCodeExchangeWidget(self.jobs_ctx, self.claimer)
        page.succeeded.connect(self._goto_page3)
        page.failed.connect(self._on_page_failure_reboot)
        self.main_layout.insertWidget(0, page)

    def _goto_page3(self):
        current_page = self.main_layout.takeAt(0).widget()
        current_page.hide()
        current_page.setParent(None)
        page = ClaimUserProvideInfoWidget(self.jobs_ctx, self.claimer, self.user_email)
        page.succeeded.connect(self._goto_page4)
        page.failed.connect(self._on_page_failure_reboot)
        self.main_layout.insertWidget(0, page)

    def _goto_page4(self):
        current_page = self.main_layout.itemAt(0).widget()
        assert current_page
        new_device = current_page.new_device
        current_page.hide()
        current_page.setParent(None)
        page = ClaimUserFinalizeWidget(self.config, new_device)
        page.succeeded.connect(self._on_finished)
        page.failed.connect(self._on_page_failure_stop)
        self.main_layout.insertWidget(0, page)

    def _on_finished(self, device, password):
        show_info(self, _("TEXT_CLAIM_USER_SUCCESSFUL"))
        self.status = (device, password)
        self.dialog.accept()

    def _on_claimer_success(self):
        assert self.claimer_job
        assert self.claimer_job.is_finished()
        assert self.claimer_job.status == "ok"
        self.claimer_job = None

    def _on_claimer_error(self):
        assert self.claimer_job
        assert self.claimer_job.is_finished()
        assert self.claimer_job.status != "ok"
        if self.claimer_job.status != "cancelled":
            msg = ""
            exc = None
            if self.claimer_job.status == "invitation-not-found":
                msg = _("TEXT_CLAIM_USER_INVITATION_NOT_FOUND")
            else:
                msg = _("TEXT_CLAIM_USER_UNKNOWN_ERROR")
            if self.claimer_job.exc:
                exc = self.claimer_job.exc.params.get("origin", None)
            show_error(self, msg, exception=exc)
        self.claimer_job = None
        self._on_page_failure_stop()

    def cancel(self):
        item = self.main_layout.itemAt(0)
        if item:
            current_page = item.widget()
            if current_page and getattr(current_page, "cancel", None):
                current_page.cancel()
        if self.retrieve_info_job:
            self.retrieve_info_job.cancel_and_join()
        if self.claimer_job:
            self.claimer_job.cancel_and_join()

    def on_close(self):
        self.cancel()

    @classmethod
    def exec_modal(cls, jobs_ctx, config, addr, parent):
        w = cls(jobs_ctx=jobs_ctx, config=config, addr=addr)
        d = GreyedDialog(w, _("TEXT_CLAIM_USER_TITLE"), parent=parent, width=800)
        w.dialog = d
        if d.exec_() == QDialog.Accepted and w.status:
            return w.status
        return None
