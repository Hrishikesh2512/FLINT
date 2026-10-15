# shield-client

Talk to **Shield**, the app on the phone that holds everything your AIs know
and make, from any Python AI.

```python
from shield_client import Shield

shield = Shield("http://100.x.y.z:8791", token="…", agent="my-ai")
shield.save_note("/Research/Goa", "Hotels", "Taj is ₹8,400 a night", tags=["travel"])
for item in shield.search("goa hotel"):
    print(item.name, item.created_by)
```

The address and token are in Shield → Settings, along with a line to copy.
Over Tailscale, the address works from anywhere.

`agent` names your AI. Everything it adds or changes is marked with that name
in Shield, so you can always see which AI did what. Nothing is deleted for good
through the API, `trash()` moves to the Trash, which only you empty.

Run the tests against a real phone (`SHIELD_URL`, `SHIELD_TOKEN`), or without
one, the offline tests use a fake server.
