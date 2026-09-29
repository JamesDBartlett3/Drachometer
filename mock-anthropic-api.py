"""
mock-anthropic-api.py

A local mock server that emulates Anthropic's Claude 3.5 Sonnet API endpoint with
Server-Sent Events (SSE). It intercepts requests from the Claude Code CLI and 
streams back hardcoded token statistics to trigger Drachometer's usage hooks 
without incurring actual API billing costs.

Requirements:
   $ pip install fastapi uvicorn

Usage Instructions:

[Automated Test Mode]
To automatically spin up the proxy, temporarily override Claude CLI's environment 
variables, fire 3 test prompts, check the database, and shut down:
   $ python mock-anthropic-api.py --test

[Manual Mode]
1. Start this script to spin up the local proxy (listens on all interfaces at port 8787).
   $ python mock-anthropic-api.py

2. In a separate terminal, export the ANTHROPIC_BASE_URL to point to this proxy 
   and pass a dummy API key to bypass Claude Code's login prompt.

   
   Windows (PowerShell):
   $env:ANTHROPIC_BASE_URL="http://127.0.0.1:8787"
   $env:CLAUDE_API_KEY="sk-test-key"
   echo "test prompt" | claude -p

   Linux/WSL:
   export ANTHROPIC_BASE_URL="http://10.42.0.204:8787" # (Use the host Windows node IP if jumping OSes)
   export CLAUDE_API_KEY="sk-test-key"
   echo "test prompt" | claude -p

Every successful run against this mock will insert randomized input and
output token counts (between 10-100) into your Drachometer SQLite database.
"""

import os
import random
import json
import logging
import sys
from contextlib import asynccontextmanager

try:
    from fastapi import FastAPI, Request
    from fastapi.responses import StreamingResponse
except ImportError:
    print("Error: Required dependencies not found.")
    print("Please install them using: pip install fastapi uvicorn")
    sys.exit(1)

# Logging setup
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def _load_mock_models():
    pricing_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "drachometer-pricing.json")
    try:
        with open(pricing_path) as f:
            tiers = json.load(f).get("tiers", {})
        models = [entry.get("model") for entry in tiers.values() if isinstance(entry, dict) and entry.get("model")]
        if models:
            return models
    except Exception as e:
        logging.warning(f"Could not load pricing tiers ({e}); mock models unavailable.")
    raise RuntimeError("drachometer-pricing.json must define a model name for each tier")

MOCK_MODELS = _load_mock_models()

# --- FastAPI Lifespan and App ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.info("Starting up mock Anthropic proxy server for Claude Code...")
    yield
    logging.info("Shutting down proxy server...")

app = FastAPI(lifespan=lifespan)

@app.post("/v1/messages")
async def messages_proxy(request: Request):
    """
    Main endpoint that receives requests from Claude Code and returns a canned
    Anthropic-formatted response.
    """
    body = await request.json()
    model_name = body.get("model", "claude-3-5-sonnet-20241022")
    logging.info(f"Received request for model {model_name}")

    if body.get("stream", False):
        async def stream_generator():
            input_tokens = random.randint(10, 100)
            output_tokens = random.randint(10, 100)
            
            # Make cache numbers more realistic (Claude often hits ~90% cache read)
            # Example realistic distribution: 25k cached reads, 1k inputs, 3k cached creation
            base_scale = random.randint(5000, 30000)
            cache_read_input_tokens = int(base_scale * random.uniform(0.85, 0.98))
            cache_creation_input_tokens = int(base_scale * random.uniform(0.0, 0.05))
            input_tokens = base_scale - cache_read_input_tokens - cache_creation_input_tokens
            if input_tokens < 0:
                input_tokens = 0
            
            tool_name = random.choice([None, "read_file", "run_in_terminal", "grep_search"])
            
            # Form stop reason based on whether a tool call was made
            stop_reason = "tool_use" if tool_name else "end_turn"
            
            if tool_name:
                pass
                # We do not mock log-usage directly anymore to avoid fake session generation. 
                # This ensures the DB reflects genuine stateless multi-subprocess invocations.

            # Anthropic streaming format
            usage_start = {
                'input_tokens': input_tokens, 
                'output_tokens': 1,
                'cache_read_input_tokens': cache_read_input_tokens,
                'cache_creation_input_tokens': cache_creation_input_tokens
            }
            yield "event: message_start\n"
            yield f"data: {json.dumps({'type': 'message_start', 'message': {'id': 'msg_123', 'type': 'message', 'role': 'assistant', 'model': model_name, 'content': [], 'stop_reason': None, 'stop_sequence': None, 'usage': usage_start}})}\n\n"
            
            yield "event: content_block_start\n"
            if tool_name:
                yield f"data: {json.dumps({'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'tool_use', 'id': 'tool_1', 'name': tool_name, 'input': {}}})}\n\n"
            else:
                yield f"data: {json.dumps({'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}})}\n\n"
            
            yield "event: content_block_delta\n"
            if tool_name:
                yield f"data: {json.dumps({'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'input_json_delta', 'partial_json': '{}'}})}\n\n"
            else:
                yield f"data: {json.dumps({'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': f'Hello from the proxy! I used {input_tokens} input tokens, read {cache_read_input_tokens} cached tokens and output {output_tokens} output tokens.'}})}\n\n"
            
            yield "event: content_block_stop\n"
            yield f"data: {json.dumps({'type': 'content_block_stop', 'index': 0})}\n\n"
            
            yield "event: message_delta\n"
            yield f"data: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': stop_reason, 'stop_sequence': None}, 'usage': {'output_tokens': output_tokens}})}\n\n"
            
            yield "event: message_stop\n"
            yield f"data: {json.dumps({'type': 'message_stop'})}\n\n"

        return StreamingResponse(stream_generator(), media_type="text/event-stream")
    
    output_tokens = random.randint(10, 100)
    
    # Make cache numbers more realistic (Claude often hits ~90% cache read)
    base_scale = random.randint(5000, 30000)
    cache_read_input_tokens = int(base_scale * random.uniform(0.85, 0.98))
    cache_creation_input_tokens = int(base_scale * random.uniform(0.0, 0.05))
    input_tokens = base_scale - cache_read_input_tokens - cache_creation_input_tokens
    if input_tokens < 0:
        input_tokens = 0
    
    tool_name = random.choice([None, "read_file", "run_in_terminal", "grep_search"])
    stop_reason = "tool_use" if tool_name else "end_turn"
    
    if tool_name:
        pass
        # We do not mock log-usage directly anymore to avoid fake session generation. 
        # This ensures the DB reflects genuine stateless multi-subprocess invocations.

    content_block = {
        "type": "tool_use",
        "id": "tool_1",
        "name": tool_name,
        "input": {}
    } if tool_name else {
        "type": "text",
        "text": f"Hello from the proxy! I used {input_tokens} input tokens, read {cache_read_input_tokens} cached tokens and output {output_tokens} output tokens."
    }

    # Non-streaming response format
    return {
        "id": "msg_mock123",
        "type": "message",
        "role": "assistant",
        "model": model_name,
        "content": [content_block],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": cache_read_input_tokens,
            "cache_creation_input_tokens": cache_creation_input_tokens
        }
    }

@app.get("/health")
async def health_check():
    return {"status": "ok"}

def run_test():
    """
    Run an automated test against the mock API using Claude CLI.
    This temporarily overrides environment variables to ensure `claude` 
    points to this local mock server, bypassing normal Anthropic checks.
    """
    import subprocess
    import sys
    
    print("\n--- Running Automated Drachometer Mock Test ---")
    
    # Store original environment variables to restore them later
    original_base = os.environ.get("ANTHROPIC_BASE_URL")
    original_key = os.environ.get("ANTHROPIC_API_KEY")
    
    # Override for our test
    os.environ["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:8787"
    os.environ["ANTHROPIC_API_KEY"] = "sk-test-mock-key"
    
    try:
        # Give the proxy a brief moment to finish spinning up a worker
        import time
        import uuid
        time.sleep(1)

        # Simulate a multi-turn session by feeding mock usage payloads to the same
        # hooks Claude invokes internally, all sharing one session ID.
        test_session = str(uuid.uuid4())

        print(f"Simulating a 3-turn session: {test_session}")
        for i in range(1, 4):
            print(f"Simulating Turn {i}/3...")
            tool_name = random.choice([None, "read_file", "run_in_terminal", "grep_search"])
            if tool_name:
                payload = json.dumps({
                    "session_id": test_session,
                    "turn_id": f"turn-{i}",
                    "tool": {
                        "name": tool_name,
                        "input": {"mock": "data"}
                    },
                    "result": {
                        "exit_code": 0
                    }
                })
                subprocess.run(
                    [sys.executable, "hooks/drachometer-log-usage.py", "post-tool-use"],
                    input=payload,
                    text=True,
                    capture_output=True
                )
            
            # Second, trigger the 'stop' event hook which calculates usage just like Claude ending its stream!
            base_scale = random.randint(5000, 30000)
            cache_read_input_tokens = int(base_scale * random.uniform(0.85, 0.98))
            cache_creation_input_tokens = int(base_scale * random.uniform(0.0, 0.05))
            input_tokens = max(0, base_scale - cache_read_input_tokens - cache_creation_input_tokens)
            output_tokens = random.randint(10, 100)
            
            payload = json.dumps({
                "session_id": test_session,
                "message": {
                    "usage": {
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "cache_read_input_tokens": cache_read_input_tokens,
                        "cache_creation_input_tokens": cache_creation_input_tokens
                    },
                    "stop_reason": "tool_use" if tool_name else "end_turn"
                },
                "model": random.choice(MOCK_MODELS),
                "cwd": os.getcwd(),
                "git_branch": "test-mock-branch"
            })
            subprocess.run(
                [sys.executable, "hooks/drachometer-log-usage.py", "stop"],
                input=payload,
                text=True,
                capture_output=True
            )
            time.sleep(0.5)

        print("\nChecking Drachometer SQLite Database for tool_calls...")
        db_path = os.path.expanduser('~/.claude/drachometer.db')
        if os.path.exists(db_path):
            import sqlite3
            try:
                db = sqlite3.connect(db_path)
                calls = db.execute("SELECT id, session_id, turn_id, tool_name, recorded_at FROM tool_calls ORDER BY id DESC LIMIT 3;").fetchall()
                if calls:
                    print("Found recent tool calls in DB:")
                    for call in calls:
                        print(f"  - ID: {call[0]} | Tool: {call[3]} | Time: {call[4]}")
                else:
                    print("No tool calls found recently. (Note: Tool selection is randomized, so it's possible no tools were picked in 3 runs).")
            except Exception as e:
                print(f"Failed to read DB: {e}")
            finally:
                db.close()
        else:
            print(f"Database not found at {db_path}")
            
    finally:
        # Restore original environment to not pollute the shell
        if original_base is not None:
            os.environ["ANTHROPIC_BASE_URL"] = original_base
        else:
            del os.environ["ANTHROPIC_BASE_URL"]
            
        if original_key is not None:
            os.environ["ANTHROPIC_API_KEY"] = original_key
        else:
            del os.environ["ANTHROPIC_API_KEY"]
        
        print("--- Test Complete ---\n")

if __name__ == "__main__":
    try:
        import uvicorn
    except ImportError:
        print("Error: Required dependencies not found.")
        print("Please install them using: pip install fastapi uvicorn")
        sys.exit(1)
        
    import sys
    import threading
    
    if "--test" in sys.argv:
        # Run proxy in background thread so we can test it locally
        server_thread = threading.Thread(
            target=uvicorn.run, 
            args=(app,), 
            kwargs={"host": "127.0.0.1", "port": 8787, "log_level": "error"},
            daemon=True
        )
        server_thread.start()
        # Run test
        run_test()
        # Exit when test is done
        sys.exit(0)
    else:
        # Normal blocking mode
        uvicorn.run(app, host="0.0.0.0", port=8787)
