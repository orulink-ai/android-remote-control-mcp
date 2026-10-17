"""Explicit, excluded cleanup between PI trials; never chooses task navigation."""

from __future__ import annotations
import argparse
import time

from config import output_dir
from run_pi_trial import require_device, validate_new_trial


def reset(trial_id):
    # Validate all operator inputs before constructing a device client.
    validate_new_trial(trial_id, output_dir())
    require_device()
    from controller import PhoneRuntime

    runtime = PhoneRuntime(trial_id)
    try:
        state = runtime.observe()
        if state["state"]["observation"]["kind"] == "detail":
            for role, key in [("like", "liked"), ("collect", "collected")]:
                if state["state"]["observation"][key]:
                    action = next(a for a in runtime.actions.values() if a.get("role") == role)
                    runtime.call("android_click_node", {"node_id": action["id"]})
                    time.sleep(0.4)
                    state = runtime.observe()
            observed = state["state"]["observation"]
            if observed["liked"] is not False or observed["collected"] is not False:
                raise RuntimeError("Baseline reset could not verify unliked/uncollected")
            message = "Baseline reset verified: unliked/uncollected"
        else:
            message = "No target detail shown; no toggle cleanup performed"
        runtime.finish({"excludedSetup": True})
        return message
    finally:
        runtime.mcp.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trial_id")
    args = parser.parse_args(argv)
    print(reset(args.trial_id))


if __name__ == "__main__":
    main()
