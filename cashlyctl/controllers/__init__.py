from .base import CommandRouter
from . import system, files

def build_router():
    router = CommandRouter()
    router.register_module(system)
    router.register_module(files)
    return router
