import logging

__version__ = "0.6.6"
version = tuple(__version__.split('.'))

logging.getLogger(__name__).addHandler(logging.NullHandler())