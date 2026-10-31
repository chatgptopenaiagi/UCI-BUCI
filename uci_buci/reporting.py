from . import BUCI, TAGLINE, __version__
from .objectives import select


def status(state: dict) -> dict:
    return {"project": state["project_name"], "version": __version__, "generation": state["generation"],
            "phase": state["phase"], "identity": {"UCI": "Universal Continuous Intelligence", "BUCI": list(BUCI), "tagline": TAGLINE},
            "objectives": {key: sum(x["status"] == key for x in state["objectives"]) for key in ("pending", "running", "done", "blocked", "cancelled")},
            "next_objective": select(state), "recent_verifications": state["verifications"][-5:],
            "recent_checkpoints": state["checkpoints"][-3:], "consecutive_failures": state["consecutive_failures"]}
