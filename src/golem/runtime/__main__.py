import os
from collections.abc import Mapping, Sequence

from langchain_core.callbacks import BaseCallbackHandler

from golem.runtime.deepagents_runner import DeepAgentsRunner, gateway_model
from golem.runtime.tools import toolbox_from_env
from golem.runtime.tracing import configure_tracing, traced_main


def deepagents_runner(
    environ: Mapping[str, str], callbacks: Sequence[BaseCallbackHandler] = ()
) -> DeepAgentsRunner:
    return DeepAgentsRunner(
        model=gateway_model(environ), toolbox=toolbox_from_env(environ), callbacks=tuple(callbacks)
    )


raise SystemExit(traced_main(os.environ, deepagents_runner, configure_tracing(os.environ)))
