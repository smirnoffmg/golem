import os
from collections.abc import Mapping

from golem.runtime.deepagents_runner import DeepAgentsRunner, gateway_model
from golem.runtime.main import main


def deepagents_runner(environ: Mapping[str, str]) -> DeepAgentsRunner:
    return DeepAgentsRunner(model=gateway_model(environ))


raise SystemExit(main(os.environ, deepagents_runner))
