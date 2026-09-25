# Golem documentation

## For people who use Golem

| Page | For |
| --- | --- |
| [Getting started](guide/getting-started.md) | a first result from the web UI: sign in, give an agent a goal, open its merge request |
| [Reviewing proposals](guide/reviewing-proposals.md) | gate owners: accept, reject, or retry an agent's merge request |
| [Channels](guide/channels.md) | starting runs from Jira labels and Mattermost's `/golem` |
| [Discovering agents](guide/discovering-agents.md) | callers of other platforms: list the agents, verify a card, call one |
| [Writing an agent](guide/writing-an-agent.md) | agent authors: the catalog, roles, rules, the golden set and the CI gate |

## For people who run Golem

| Page | For |
| --- | --- |
| [Install](operations/install.md) | from an empty cluster to a first run |
| [Configuration](operations/configuration.md) | every setting, Secret and configuration file |
| [Security](operations/security.md) | trust boundaries, key rotation, what to review |
| [Backup and restore](operations/backup-and-restore.md) | what to back up, how to restore consistently |
| [Upgrade](operations/upgrade.md) | new images, schema changes, rollback |
| [Metrics and alerts](operations/alerts.md) | the metrics, the alert rules, the dashboards |
| [Runbooks](operations/runbooks.md) | one entry per alert |
| [Troubleshooting](operations/troubleshooting.md) | symptoms, causes, checks |
| [Kubernetes manifests](../deploy/k8s/README.md) | the base, its placeholders, the network check |

## Design

[Architecture](architecture.md) (C4 diagrams) and the [decision records](adr).

## How these pages are written

The guides follow the path to a first result and show the commands with their outputs; a
Getting Started guide outlines "the easiest and fastest set of steps", does not "diverge from
the happy path" but links "to troubleshooting documentation", and "if there is an expected
result, show[s] a screenshot of that result" (*Designing Web APIs*, Jin, Sahni, Shevat, с. 164
(PDF 178)). The references are complete and repeat what a reader needs, since "these types of
documents are not intended to be read in sequence" (the same, с. 166 (PDF 180)).

`tests/test_docs.py` checks these pages: links and images resolve, the settings reference
matches the parsers, the metrics and alerts named exist, and the secret generation of the
install guide produces values the processes accept.
