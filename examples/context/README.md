# Example context repository

A small discovery context for the agent in [`../discovery/agent.yaml`](../discovery/agent.yaml):
problem hypotheses in `hypotheses/`, solutions that link to them in `solutions/`. The lead reads
it through `golem.runtime.snapshot.build_snapshot` and picks the next role with
`golem.runtime.lead.decide`.

## Record format

- **One record per Markdown file** (`*.md`) anywhere in the tree. Directories whose name starts
  with a dot are skipped, and so is any file that does not open with front matter, like this
  README.
- **Front matter** is YAML between a first line `---` and the next `---` line:

  ```yaml
  ---
  id: S-1            # required, ^[A-Za-z][A-Za-z0-9-]*$, unique in the repository
  kind: solution     # required, matched by the `kind` of catalog rules
  status: proposed   # required, matched by the `statuses` of catalog rules
  links: [H-1]       # optional, ids of existing records
  owner: product     # any other key is allowed and ignored
  ---
  ```

- **Sections** are level-2 headings (`## Evidence`). Text before the first one belongs to no
  section; `###` headings inside a section are its content. A section is **empty** when nothing
  but whitespace and HTML comments remains. Leave a comment as a prompt for the role that fills
  the section in:

  ```markdown
  ## Evidence

  <!-- What supports or refutes the problem? Link sources. -->
  ```

Sections are the unit a role fills in: the catalog condition `empty_section` holds while a
section is empty, and `no_linked` holds while no record of a given kind and status links to the
target.

## Validation

The same function runs in this repository's CI. It fails with the file path and the problem on
malformed or unclosed front matter, a missing or invalid `id`, `kind` or `status`, `links` that
are not a list of ids, a duplicate id (naming both files) and a link to an id that does not exist.

## What the lead does here

With the rules of the discovery agent, in priority order:

1. `researcher` on a `proposed` hypothesis whose `Evidence` is empty: **H-2**.
2. `designer` on a `validated` hypothesis that no `proposed` or `accepted` solution links to:
   **H-3** (H-1 already has S-1).
3. `reviewer` on a `proposed` solution whose `Review` is empty: **S-1**.

While H-2 has an open merge request (it is pending), the lead moves on to H-3, and so on.
