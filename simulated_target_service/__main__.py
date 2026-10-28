import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run("simulated_target_service.app:app", host="127.0.0.1", port=int(os.environ.get("SIMULATED_TARGET_PORT", "8100")))
