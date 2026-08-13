# SLURM WEB — instructions for agents

*The cluster does not care what you meant to run. It runs what you posted, on
whatever node was free, and reports COMPLETED when the wrapper exits. Read the
log, not the status.*

Companion to [`ARGUS.md`](ARGUS.md). ARGUS is where you record work; this is how
you launch it. Anything you start here belongs in a TAM issue with a watch bound
to it — a job running with no board entry is invisible the moment you close your
terminal.

The service is a Flask app on **master-node01**, source in `~/dev/simple_slurm_web`
on that host (not on NFS, and SSH to it is key-only — you will not read the source
from a worker). It is an HTML UI with a thin JSON surface bolted on. There is
**no authentication and no CSRF token**: a single POST launches a real job on real
hardware. Treat every POST to `/docker_submit` as spending GPU-days.

```sh
SLURM_WEB="http://192.168.10.66:8082"
```

---

## The map

| route | method | returns | use it for |
|---|---|---|---|
| `/jobs` | GET | HTML | running + queued queue |
| `/history` | GET | HTML | finished jobs |
| `/job/<id>` | GET | HTML | one job — **embeds the entire log**, 20 MB+ |
| `/docker_submit` | GET | HTML | the submit form, and the authoritative list of images/nodes |
| `/docker_submit` | POST | 302 | **launches a job** |
| `/api/job_output/<id>` | GET | JSON | stdout+stderr — **whole file**, see below |
| `/api/cancel/<id>` | POST | JSON | cancel |
| `/create_test_job` | GET | 302 | **submits a job on GET** — see below |
| `/nodes` | GET | — | currently times out; use the form's node dropdown instead |

Those two `/api/` routes are the only JSON. Everything else is scraped HTML, so
parse defensively and expect it to change.

---

## Read logs from disk, not from the API

`/nfs` is mounted on the workers from master-node01, and every job writes to it:

```sh
/nfs/slurm_logs/docker_job_<id>.out
/nfs/slurm_logs/docker_job_<id>.err
```

Use those. `GET /api/job_output/<id>` returns the *complete* stdout and stderr in
one JSON blob — measured at 23 MB and 140 MB on two live training jobs. Polling it
on a timer will hurt the master node and tell you nothing that `tail` would not.

**Training progress is on stderr, not stdout.** On a YOLOv7 job the split was
stdout 11 KB, stderr 20 MB — the epoch lines, the per-class table and any library
warning all land in `.err`. Grep the wrong file and a healthy run looks silent.

```sh
tail -c 20000 /nfs/slurm_logs/docker_job_38582.err | grep -aE "^ +[0-9]+/[0-9]+"
grep -ac "CUDA out of memory\|Traceback" /nfs/slurm_logs/docker_job_38582.err
```

Pass `-a` to grep: the logs carry progress-bar control characters and grep will
otherwise decide they are binary and say nothing.

---

## Submitting

Form-encoded POST to `/docker_submit`. Fields, with the defaults the form ships:

| field | default | notes |
|---|---|---|
| `is_docker_job` | `true` | hidden, required |
| `job_name` | `docker_job` | **set this** — everything is named `docker_job` otherwise |
| `partition` | `compute` | or `debug` |
| `worker_node` | *(empty = auto)* | pin it when the job needs a specific GPU |
| `cpus` | `4` | |
| `memory` | `8` | GB |
| `time` | `30-00:00:00` | |
| `gpu_enabled` | *(unchecked)* | checkbox — send `on` |
| `gpu_count` | `1` | |
| `docker_image` | — | see the form for the current list |
| `pull_latest` | *(unchecked)* | |
| `mount_points` | `/mnt/datasets:/mnt/datasets` | one per line |
| `cache_dataset` | *(unchecked)* | copies the dataset to node-local disk first |
| `dataset_source` | a VisDrone path | the tree to cache |
| `use_github` | *(unchecked)* | clone a repo into the container |
| `github_repo` | `git@github-enterprise.com:Controp-NT/general_trainer.git` | |
| `github_branch` | `main` | **the branch is what actually runs — push first** |
| `cli_command` | a cityscapes example | the command run inside the container |
| `extra_repo_url[]`, `extra_repo_dest[]` | — | repeatable pairs |

`GET /docker_submit` is the source of truth for which images and nodes exist
today. Scrape the `<select>` options rather than trusting a list written down
anywhere, including this file.

### A submission that works

This one ran. Copy it and change the command, not the scaffolding:

```sh
CMD='python train.py --name my_run \
  --weights /mnt/datasets/checkpoints/detection/yolov7/yolov7_training.pt \
  --cfg cfg/models/training/yolov7.yaml \
  --data /workspace/data/detection/<dataset>/data.yaml \
  --hyp cfg/hyperparameters/<project>/<hyp>.yaml \
  --batch-size 16 --img-size 768 768 --workers 4 --epochs 300 --patience 100 \
  --device 0 --project /mnt/datasets/training_workdirs/ --clearml --exist-ok'

curl -s -o /dev/null -w "%{http_code} -> %{redirect_url}\n" \
  -X POST "$SLURM_WEB/docker_submit" \
  --data-urlencode "is_docker_job=true" \
  --data-urlencode "job_name=my_run" \
  --data-urlencode "partition=compute" \
  --data-urlencode "worker_node=worker-node01" \
  --data-urlencode "cpus=4" \
  --data-urlencode "memory=16" \
  --data-urlencode "time=30-00:00:00" \
  --data-urlencode "gpu_enabled=on" \
  --data-urlencode "gpu_count=1" \
  --data-urlencode "docker_image=master-node01:5000/general_trainer:latest" \
  --data-urlencode "pull_latest=on" \
  --data-urlencode "mount_points=/mnt/datasets:/mnt/datasets" \
  --data-urlencode "cache_dataset=on" \
  --data-urlencode "dataset_source=/mnt/datasets/data/detection/<dataset>" \
  --data-urlencode "use_github=on" \
  --data-urlencode "github_repo=git@github-enterprise.com:Controp-NT/SVTrainer.git" \
  --data-urlencode "github_branch=<your-branch>" \
  --data-urlencode "cli_command=$CMD"
```

**The job id comes back in the redirect**, not the body: `302 -> .../job/38596`.
Capture it with `-w "%{redirect_url}"` as above — you need it for the ARGUS watch
and for `/api/cancel/<id>`.

Note the path asymmetry in that command, which is easy to get wrong: `--data` is a
**container** path (`/workspace/data/...`) because the dataset is mounted there,
while `--weights` and `--project` are **host** paths under `/mnt/datasets` because
that mount is passed through unchanged.

**Pass `--clearml`.** The image ships its own ClearML credentials — you do NOT need
the `CLEARML_*` env vars the shell scripts export, and omitting the flag silently
costs you the experiment tracking. The log prints `ClearML results page: <url>` when
it works.

### Verify the job got what you meant

Do this every time, within a minute of submitting. The container clones from git, so
a stale branch, a typo in a `--hyp` path, or an argument the trainer silently ignores
all look identical to success until you read the log:

```sh
curl -s "$SLURM_WEB/api/job_output/<id>" | python -c "
import json,sys,re
d=json.load(sys.stdin); e=(d.get('stdout') or '')+(d.get('stderr') or '')
print('state:', d.get('job_state'))
for pat in [r'Namespace\(.{0,200}', r'hyperparameters: .{0,200}', r'ClearML results page: \S+']:
    m=re.search(pat,e); print(' ', m.group(0) if m else '(missing: '+pat+')')"
```

Read the `hyperparameters:` line specifically and confirm the values you changed are
actually there. A hyp key the trainer does not read produces no error at all.

Restarting is nearly free at epoch 0 and expensive later, so check immediately.

### Cache the dataset — otherwise every epoch reads over NFS

`cache_dataset` + `dataset_source` copy the tree to the worker's local disk before
the container starts, and the job then reads images from there instead of pulling
them over NFS from master-node01 every epoch. On a 187k-file detection set that is
the difference between a local read and a network round trip per image, every
epoch, for 300 epochs.

```
cache_dataset=on
dataset_source=/mnt/datasets/data/detection/project_2d_navy_v6_non_dup_0.97_fixed_barges
```

It is incremental: the wrapper hashes the source and compares file counts, so a
second job against the same dataset prints `Dataset cache is up to date` and starts
immediately. The copy lands at `/home/<user>/data/detection/<set>` on the worker.
Note that the cache is per node — pinning a follow-up run to the same
`worker_node` reuses it; a different node pays the copy again.

### Paths differ inside the container

The host path is **not** the container path. A job whose host dataset lives at
`/mnt/datasets/data/detection/<set>` referred to it as
`/workspace/data/detection/<set>` in its own `opt.yaml`. The repo is cloned to
`/workspace`, which is also `$HOME` for the container user. Write `cli_command`
in container paths, and check a recent job's log before guessing:

```sh
grep -m1 -a "Namespace(" /nfs/slurm_logs/docker_job_<recent>.err
```

### The clone is from git, not from your working tree

`use_github` clones `github_branch` fresh inside the container. Uncommitted work
on your laptop does not exist to the job. If a run's config does not match what
you edited, this is why — check the commit the branch pointed at when it started.

---

## Cluster shape (verified 2026-08-12 — re-check, nodes drift)

| node | GPU | state |
|---|---|---|
| worker-node01 | RTX 4090, 24 GB | mix |
| worker-node03 | RTX A6000, 48 GB | mix |
| worker-node04 | RTX A6000, 48 GB | idle |
| worker-node02 | — | down |
| master-node01 | — | down |

Two 24 GB cards and two 48 GB cards is the whole story: a job that needs more than
24 GB must pin `worker_node` to node03 or node04, or it will land on the 4090 and
die at the first large batch. Auto-select does not know what your job needs.

---

## Rules

**Bind an ARGUS watch to every job you submit.** Get the job id from the redirect
or from `/jobs`, then follow ARGUS §4. Include `state_patterns` — Slurm reports
`COMPLETED` whenever the wrapper script exits 0, *including when the training
inside it was killed*, so without the pattern an aborted run reads as success.

**Never pin to a down node.** A dozen `nvidia-smi` jobs sat PENDING against
`master-node01` and `worker-node02` for days, cluttering the queue for everyone.
Check the form's node dropdown for `(down*)` before setting `worker_node`.

**Name your job.** `docker_job` × 20 in the history is unreadable. The name is
free and permanent.

**Cancel through the API, and say why on the issue.** `POST /api/cancel/<id>`
kills it; a comment saying what you saw is what stops the next person repeating it.

**`/create_test_job` submits on GET.** It is not a form page — fetching the URL
queues a real (trivial, ~15 s) job and redirects. Do not include it when probing
routes; it cost job 38592 to learn this. `/docker_submit` and `/docker/build` are
safe to GET, and only act on POST.

**Do not submit to "just check something".** The `debug` partition exists for that,
and reading `/nfs/slurm_logs/` answers most questions without launching anything.
