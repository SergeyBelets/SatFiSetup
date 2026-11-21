"""Authenticated local cache of public evidence, scoped to one CLI invocation.

Never caches mutable permissions or a successful live Bitcoin observation.
Cache entries remain subject to canonical block and current runtime checks.
"""
from contextvars import ContextVar
from web3 import Web3
from .operations import canonical

cache_state = ContextVar("verified_public_cache", default=None)


def key(w3, namespace, parameters):
    return namespace + ":" + Web3.keccak(text=canonical([
        str(w3.eth.chain_id), parameters])).hex()


def get(w3, namespace, parameters):
    state = cache_state.get()
    return state.get(key(w3, namespace, parameters)) if state is not None else None


def put(w3, namespace, parameters, value):
    state = cache_state.get()
    if state is not None:
        with state.exclusive():
            state.put(key(w3, namespace, parameters), value)
