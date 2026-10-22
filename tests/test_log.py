import logging

from vmdash.log import setup_logging


def test_file_handler_writes(tmp_path):
    path = tmp_path / "v.log"
    setup_logging("DEBUG", path)
    logging.getLogger("vmdash.test").info("hello from test")
    for h in logging.getLogger().handlers:
        h.flush()
    assert "INFO vmdash.test: hello from test" in path.read_text()


def test_level_applied():
    setup_logging("WARNING", None)
    assert logging.getLogger().level == logging.WARNING


def test_idempotent(tmp_path):
    setup_logging("INFO", None)
    setup_logging("INFO", None)
    assert len(logging.getLogger().handlers) == 1
