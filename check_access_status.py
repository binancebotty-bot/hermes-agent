from hermes_cli.webapp_access import access_status
import json
print(json.dumps(access_status(), indent=2))