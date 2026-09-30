import logging
import sys

from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication

from midi_mixer.constants import APP_NAME, SINGLETON_NAME
from midi_mixer.theme import STYLESHEET
from midi_mixer.window import MainWindow


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)  # keep running in the tray
    app.setStyleSheet(STYLESHEET)

    # Single instance: if one is already running, ask it to show its window and leave.
    sock = QLocalSocket()
    sock.connectToServer(SINGLETON_NAME)
    if sock.waitForConnected(300):
        sock.write(b"show")
        sock.flush()
        sock.waitForBytesWritten(300)
        return 0
    QLocalServer.removeServer(SINGLETON_NAME)  # stale socket from a crashed run
    server = QLocalServer()
    server.listen(SINGLETON_NAME)

    window = MainWindow(start_hidden="--minimized" in sys.argv)
    window.single_server = server
    server.newConnection.connect(window._on_new_instance)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
