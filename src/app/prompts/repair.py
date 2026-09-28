REPAIR_SYSTEM_PROMPT = (
    "You output only valid JSON. Never include markdown fences or prose.\n"
    "Use n8n's canonical workflow shape: `nodes` is an ARRAY of node objects; "
    "`connections` is an OBJECT keyed by source node NAME with targets referenced "
    "by node name inside a `\"main\": [[...]]` array-of-arrays; node `position` is "
    "a `[x, y]` array."
)