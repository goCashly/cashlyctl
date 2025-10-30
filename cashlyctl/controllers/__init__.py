from .base import CommandRouter
from . import system, files, submit, help

def build_router():
    router = CommandRouter()
    router.register_module(system)
    router.register_module(files)
    router.register_module(submit)
    router.register_module(help)
    return router
