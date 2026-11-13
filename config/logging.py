"""Standard-library logging: one console stream, every record tagged with the request ID."""

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {"request_id": {"()": "core.request_logging.RequestIdFilter"}},
    "formatters": {
        "standard": {
            "format": "%(asctime)s %(levelname)s %(name)s request_id=%(request_id)s "
            "render_request_id=%(render_request_id)s %(message)s"
        }
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",
            "filters": ["request_id"],
            "formatter": "standard",
        },
        "discard": {"class": "logging.NullHandler"},
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    # Replace Django's default handlers (including mail_admins) so records reach the root only.
    "loggers": {
        "django": {"handlers": [], "level": "INFO"},
        # runserver's own request line logs the raw path and query; the middleware replaces it.
        "django.server": {"handlers": ["discard"], "level": "INFO", "propagate": False},
    },
}
