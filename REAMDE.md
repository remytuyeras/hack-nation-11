# Installation

Use one or the other:
```bash
source build_sdk.sh reset && bash install_requirements.sh
```
or
```bash
bash build_sdk.sh reset && bash install_requirements.sh
source venv/bin/activate
```

# How to run

Make sure every terminal is its the `venv` environment

```bash
source venv/bin/activate
```

```bash
# Terminal 1 (server configured for MMO) make sure only 1 server run in local host
python server.py 

# Terminal 2 (game master)
python agents/agent_GameMaster/agent.py

# Terminal 3 (player 1)
python agents/agent_GamePlayer/agent.py --id alice

# Terminal 4 (player 2)
python agents/agent_GamePlayer/agent.py --id bob
```
