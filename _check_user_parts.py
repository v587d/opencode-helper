"""Investigate user message part structure."""
import sys, json
sys.path.insert(0, ".")
from drilldown.graph import _get_messages, _get_parts_with_message

sid = "ses_1319c7155ffe28gAofT9DQM0Z0"
parts = _get_parts_with_message(sid)
messages = _get_messages(sid)

# Find user messages and their parts
for msg in messages:
    if msg.get("role") != "user":
        continue
    mid = msg["id"]
    msg_parts = [p for p in parts if p["message_id"] == mid]
    print(f"\n=== User msg {mid[:30]}... ===")
    print(f"  total parts: {len(msg_parts)}")
    print(f"  msg keys: {[k for k in msg.keys() if k not in ('id','msg_time','time')]}")
    for i, p in enumerate(msg_parts):
        ptype = p.get("type", "?")
        text = p.get("text", "")
        # Show all keys in the part
        print(f"  part[{i}] type={ptype} | keys={list(p.keys())}")
        if ptype == "text" and text:
            # Show first 200 chars
            preview = text[:200].replace('\n', '\\n')
            print(f"    text preview: {preview}...")
            print(f"    total chars: {len(text)}")
