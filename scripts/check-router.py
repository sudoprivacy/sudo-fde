"""Read-only acceptance against a real Router; never reserves quota or calls a model."""
import argparse
import json

from fde_server.config import Settings
from fde_server.router import SudoRouterClient

parser = argparse.ArgumentParser()
parser.add_argument("--account", required=True)
args = parser.parse_args()
router = SudoRouterClient(Settings())
capabilities = router.capabilities()
if capabilities.get("simulation"):
    raise SystemExit("Refused: this is a simulator, not real Router acceptance")
account = router.account(args.account)
print(json.dumps({"contract": capabilities["contract"], "account_id": account["account_id"],
                  "models": account["models"], "balance_query": "passed", "paid_execution": "not_performed"}, indent=2))
