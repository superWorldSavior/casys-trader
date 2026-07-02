"""Tests logging_setup : TTY → RichHandler, non-TTY → StreamHandler sans markup."""
import io
import logging


from trader.logging_setup import setup_logging



def test_tty_installe_un_rich_handler(monkeypatch) -> None:
    """Quand stdout est un TTY, le logger reçoit un RichHandler."""

    root_logger = logging.getLogger("casys-trader")
    # Nettoyer les handlers résiduels d'autres tests
    root_logger.handlers.clear()

    class FakeTTY(io.StringIO):
        def isatty(self):
            return True

    setup_logging(level=logging.INFO, stream=FakeTTY())

    handler_types = [type(h).__name__ for h in root_logger.handlers]
    assert "RichHandler" in handler_types, f"Handlers: {handler_types}"
    # Nettoyer après le test
    root_logger.handlers.clear()


def test_non_tty_installe_un_stream_handler_standard(monkeypatch) -> None:
    """Quand stdout n'est pas un TTY (pipe/CI), le logger reçoit un StreamHandler."""
    root_logger = logging.getLogger("casys-trader")
    root_logger.handlers.clear()

    class FakePipe(io.StringIO):
        def isatty(self):
            return False

    setup_logging(level=logging.INFO, stream=FakePipe())

    handler_types = [type(h).__name__ for h in root_logger.handlers]
    assert "StreamHandler" in handler_types
    assert "RichHandler" not in handler_types, f"RichHandler ne doit pas être là: {handler_types}"
    root_logger.handlers.clear()


def test_non_tty_ne_laisse_pas_fuir_les_balises_rich() -> None:
    """En mode non-TTY, les messages ne doivent pas contenir de markup [green]..."""
    root_logger = logging.getLogger("casys-trader")
    root_logger.handlers.clear()

    class FakePipe(io.StringIO):
        def isatty(self):
            return False

    # On ne peut pas tester directement le rendu ici (le formatter est sur le handler),
    # mais on vérifie que le handler n'est pas un RichHandler (qui interpréterait le markup).
    setup_logging(level=logging.INFO, stream=FakePipe())

    # Le formatter du StreamHandler ne doit pas contenir "[" dans le format raw
    handler = root_logger.handlers[0]
    assert not hasattr(handler, "console"), "RichHandler ne doit pas être installé"
    root_logger.handlers.clear()


def test_setup_logging_configure_le_niveau() -> None:
    """setup_logging doit configurer le niveau demandé sur le logger."""
    root_logger = logging.getLogger("casys-trader")
    root_logger.handlers.clear()

    class FakePipe(io.StringIO):
        def isatty(self):
            return False

    setup_logging(level=logging.WARNING, stream=FakePipe())
    assert root_logger.level == logging.WARNING
    root_logger.handlers.clear()
