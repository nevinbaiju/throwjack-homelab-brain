# Focus Board Brain — Commit 0

Capture service. Appends every thought to a plaintext file on the mirrored
array and does nothing else. No model, no database.

## Start it

    cd ~/brain
    podman compose up -d --build     # or: podman-compose up -d --build

## Verify

Use the wrapper — no multi-line pastes, no token juggling:

    ./brain.sh health
    ./brain.sh capture "p2: try the brain out"
    ./brain.sh triage          # don't wait for the 10-minute timer
    ./brain.sh board
    ./brain.sh logs

The raw file is at /var/mnt/storage/brain/inbox/YYYY-MM-DD.jsonl — readable
with `cat` if every other part of the system is broken. That is the point.

## iOS Shortcut: "Dump"

One action, "Get Contents of URL":

    URL     http://<your-tailnet-ip>:5010/capture
    Method  POST
    Headers Authorization  = Bearer <token from .env>
            Content-Type   = text/plain
            X-Source       = shortcut-dump
    Body    Request Body -> Text -> (Dictated Text or Ask For Input)

Then in the shortcut settings turn OFF "Show When Run", and do NOT add a
"Show Result" action. It must close silently — a result screen is where the
thought you were about to have goes to die.

Add to Home Screen, or bind to the Action Button.

## Endpoints

    POST /capture       append a thought. Accepts JSON {"text": ...},
                        a bare JSON string, form-encoded, or raw text/plain.
                        Optional X-Source header. Returns 202.
    GET  /health        no auth. Confirms the inbox is writable.
    GET  /inbox/today   auth. Human-readable dump of today's captures.

## Projects (deep focus track)

Turn an idea dump into a sequenced plan:

    ./brain.sh dump doordash notes.md    # from a file
    ./brain.sh dump doordash             # or type it, finish with Ctrl-D

    ./brain.sh project doordash          # status, what's next, open questions
    ./brain.sh sync doordash             # pull captures/ another LLM left

At most 3 tasks per project go to In Progress; the rest wait in Backlog. The
raw dump is archived verbatim under contexts/<project>/dumps/ before anything
interprets it.

## Context sharing (Syncthing)

Only contexts/ and journal/ sync. inbox/ and state/ stay server-only.

The web UI answers on every interface:

    http://syncthing.<your-domain>      (LAN, if you run a reverse proxy + DNS rewrite)
    http://<server-lan-ip>:8384         (LAN, direct)
    http://<your-tailnet-ip>:8384       (tailnet, from anywhere)

Nothing forwards 8384 at the router, so it is not internet-exposed. But the
LAN can reach it now, so:

1. Open it and set a GUI username and password immediately
   (Actions -> Settings -> GUI). Do this before anything else.
2. On your laptop, install Syncthing and add this device by its ID
   (Actions -> Show ID here).
3. Share the `contexts` folder to it. Accept on the laptop, pick a local path
   such as ~/brain-contexts.
4. Drop contexts/AGENTS.md into a project repo (or point your assistant at it)
   so the tool knows the contract.

### Roaming laptops

Sync traffic uses 22000/tcp+udp; discovery uses 21027/udp broadcast, which
only works on the same LAN. Away from home, the laptop finds the server
through Syncthing's global discovery — or, more reliably, pin the tailnet
address on the laptop's entry for this device:

    Edit device -> Addresses:  tcp://<your-tailnet-ip>:22000, dynamic

That makes the tailnet the always-available path and the LAN the fast one when
you are home. Syncthing picks whichever connects.
