# -------------------------------------------------------
# Shared logging setup
#
# Shared logging setup for the YouTube Manager services. Every record starts with a
# "[LEVEL]  [timestamp] message" line and its continuation lines are indented, so a
# log collector that opens a new entry at each tag (a Promtail multiline stage, say)
# keeps a traceback whole.
# -------------------------------------------------------

import logging
import sys
import warnings

# The short tags WARN and CRIT, not Python's longer WARNING/CRITICAL, which would
# break the padded-tag alignment.
logging.addLevelName(logging.WARNING, 'WARN')
logging.addLevelName(logging.CRITICAL, 'CRIT')

# Width of the "[LEVEL]" field, sized to the longest tag ("[ERROR]").
_TAG_WIDTH = 7

_configured = False


# Renders records as "[LEVEL]  [YYYY-MM-DD HH:MM:SS] message".
class StackFormatter(logging.Formatter):
    default_time_format = '%Y-%m-%d %H:%M:%S'
    # Suppress the ",%03d" milliseconds suffix formatTime appends by default;
    # the line format carries whole seconds.
    default_msec_format = ''

    # Render one record, appending any traceback or stack trace indented by four
    # spaces so none of its lines can open a new log entry (see the header).
    def format(self, record: logging.LogRecord) -> str:
        tag = f'[{record.levelname}]'.ljust(_TAG_WIDTH)
        line = f'{tag} [{self.formatTime(record, self.datefmt)}] {record.getMessage()}'

        # Indenting keeps a traceback line that itself starts with "[Something]" from
        # matching a collector's first-line pattern and splitting off its own entry.
        extra = ''
        if record.exc_info:
            extra = self.formatException(record.exc_info)
        elif record.exc_text:
            extra = record.exc_text
        if record.stack_info:
            extra = f'{extra}\n{record.stack_info}' if extra else record.stack_info
        if extra:
            line += '\n' + '\n'.join(f'    {text}' for text in extra.splitlines())

        return line


# Install the shared stdout handler on the root logger. Idempotent: only the first call builds it.
def configure(level: int = logging.INFO) -> None:
    global _configured
    if _configured:
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(StackFormatter())

    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)

    # Otherwise warnings.warn() writes straight to stderr as a bare two-line
    # record that carries no level and fragments into two log entries.
    logging.captureWarnings(True)

    # The Jellyfin certificate is self-signed and trusted deliberately, so
    # urllib3 warns on every single request. Expected, and pure noise.
    # Only the import is optional; a failure in the filter itself must surface.
    try:
        from urllib3.exceptions import InsecureRequestWarning
    except ImportError:
        pass
    else:
        warnings.filterwarnings('ignore', category=InsecureRequestWarning)

    # Let uvicorn's own loggers propagate to the root handler above instead of
    # writing their own differently-formatted lines.
    for name in ('uvicorn', 'uvicorn.error', 'uvicorn.access'):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True

    _configured = True
