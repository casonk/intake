# Deployment modes: where intake parses untrusted files

Intake's daemon reads receipts that arrive from outside: email attachments and uploads to a shared folder. The
parsers for PDFs, images and videos (pdfplumber, Pillow, tesseract, ffmpeg) read content an attacker can shape,
and the daemon holds your mailbox access, API keys and database. This choice decides where those parsers run. You
choose with one setting, and **only the `k3s` mode needs a cluster**. If you just want intake, you need Python and
nothing else.

The daemon itself always runs on the host (Clockwork manages `intake-daemon.service`). What changes is where it
sends files to be parsed.

| Mode | Where parsing happens | Needs | Use it for |
|---|---|---|---|
| `dev` (default) | a sandbox service you run in the foreground on this machine | Python 3.11+ | trying the sandbox, developing |
| `host` | in the daemon's own process, as intake always has | Python, tesseract | a machine that just runs intake |
| `k3s` | in a K3s pod with no network, no secrets and none of your files | K3s, podman, kubectl, the in-cluster registry | keeping hostile files away from the daemon's keys |

The application is the same in all three. `INTAKE_EXTRACTOR` (`local` or `sandbox`) is the only switch, and its
default is `local`: see [`extractor-sandbox.md`](extractor-sandbox.md) for what it changes in the code.

## Choose a mode

```bash
deploy/deploy.sh mode                  # print the mode in effect and where it came from
deploy/deploy.sh up                    # make it so
deploy/deploy.sh status | verify | down
```

The mode is read from, first match wins:

1. `--mode dev|host|k3s` on the command line
2. `INTAKE_DEPLOY_MODE` in the environment
3. `INTAKE_DEPLOY_MODE` in `config/deploy.local.env` (copy `config/deploy.example.env`; it is gitignored)
4. `dev`

An unknown value, or a config file that cannot be read, is an error, never a silent fallback.

## What each mode does

### dev
`up` runs `python -m intake.extractor_service` in the foreground on `127.0.0.1:5455`. To use it from the daemon or
the CLI, set `INTAKE_EXTRACTOR=sandbox` and `INTAKE_EXTRACTOR_URL=http://127.0.0.1:5455`. It is the same service the
pod runs, without a container, so a developer can exercise the sandbox path with no cluster.

### host
`up` removes the sandbox drop-in from the daemon's unit and restarts the daemon, which then parses in-process
(`INTAKE_EXTRACTOR=local`). If the K3s sandbox was active it also takes the bridge down and scales the pod to 0.

### k3s
`up` refuses to start, and changes nothing, unless kubectl, podman and the in-cluster registry are present. Then it
runs, in order: `build-image.sh` (refuses uncommitted `intake/`, because the pod runs what is committed),
`apply.sh` (namespace and network policy first, then the pod), `bridge.sh on` (puts the pod behind
`127.0.0.1:5455`), `verify.sh` (must pass), and only then writes a drop-in that sets `INTAKE_EXTRACTOR=sandbox` on
`intake-daemon.service` and restarts it. If any step from the bridge onward fails, the bridge goes back down and
the daemon keeps parsing in-process: it is never left pointing at a sandbox that is not there.

## What the pod can reach

Nothing, and nothing of yours can be reached from it.

- **Network:** no DNS, no internet, no host, no Kubernetes API. Only the node can connect in, on port 5455.
- **Files:** none of yours. Its only volume is a 1 GiB scratch directory that holds each request's document for the
  duration of that request. It does not mount the receipts folder, the database or any repository.
- **Secrets:** none, and no environment variables are passed in. It has no Kubernetes token.
- **Identity:** a fixed non-root uid that owns nothing of yours, read-only root filesystem, no capabilities.
- **Per document:** each file is parsed in a child process with a CPU limit and a wall-clock timeout, and the
  service refuses oversize or unsupported files.

If the sandbox stops answering, the daemon leaves the inbox alone for that cycle and logs it. It does **not**
parse locally instead.

## Switching

| From | To | Command |
|---|---|---|
| dev | host | stop the foreground service (Ctrl-C), then `deploy/deploy.sh --mode host up` |
| host | k3s | `deploy/deploy.sh --mode k3s up` (verifies first, then switches the daemon) |
| k3s | host | `deploy/deploy.sh --mode host up` (drops the daemon back to in-process, takes the bridge down, scales the pod to 0) |

`--mode k3s down` is the same as `--mode host up`. A marker in `~/.local/state/intake/` records which mode last held
the work. Your receipts, database and settings never move: only the daemon's environment changes.

## What stays on the host

- **The daemon** and everything it integrates with: Gmail polling, the OpenAI calls, Signal notes, the career and
  personal-finance integrations. These need your credentials and other repositories, so moving them would widen the
  pod's reach instead of narrowing it.
- **`intake merge`** reads PDFs with `pypdf` in its own process; it is a manual command and is not routed through the
  sandbox.

## For contributors without K3s

- `pytest` needs no cluster: the deployment tests parse manifests and run the scripts against stub commands, and the
  service tests run the real service over loopback against a fake worker.
- Nothing under `deploy/k3s/` runs unless you select the `k3s` mode.
