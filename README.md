# Splunk Observability Cloud: host custom properties for muting rules

Muting rules in Splunk Observability Cloud can't match on a prefix or wildcard
(for example "every host whose name starts with `tf-demo-`"). The practical
alternative is to give the hosts you want to mute a shared **custom property**
(for example `mute_group = demo-muting-rule`) and build one muting rule on that
property.

`add_properties.py` does the tagging for you. It reads a list of hosts from a
CSV file and adds the properties from a config file to each host's dimension,
**keeping any custom properties, tags and description the dimension already
has**. It has a dry-run mode so you can preview every change first.

## Contents

- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Setup](#setup)
- [Config file reference](#config-file-reference)
- [Hosts CSV format](#hosts-csv-format)
- [Usage](#usage)
- [Reading the output](#reading-the-output)
- [Using the property in a muting rule](#using-the-property-in-a-muting-rule)
- [Finding the right dimension key and value](#finding-the-right-dimension-key-and-value)
- [Limits](#limits)
- [Troubleshooting](#troubleshooting)

## How it works

Splunk Observability Cloud has two kinds of metadata that matter here:

| Type | Set when | Notes |
| --- | --- | --- |
| **Dimension** | At ingest, sent with the metric | Immutable. Part of what identifies a time series. For example `host.name = ip-172-32-2-78.eu-west-1.compute.internal`. |
| **Custom property** | After ingest, via the UI or API | Attached to a dimension, so it applies to every time series that carries that dimension. Doesn't create new time series. |

Adding a custom property through the API means you don't have to change or
restart the agent or collector on the host.

For each hostname in the CSV the script:

1. Sends `GET /v2/dimension/<dimension_key>/<host>` to read the dimension's
   current `customProperties`, `tags` and `description`.
2. Merges the properties from `config.json` into the existing custom
   properties.
3. Sends `PUT /v2/dimension/<dimension_key>/<host>` with the merged result.
   (Skipped in `--dry-run` mode.)

Why it reads before it writes: the dimension `PUT` replaces the whole set of
custom properties on that dimension. Sending only your new property would wipe
out what was already there (for example the `aws_*` properties from the AWS
integration). Reading first and sending everything back avoids that.

How conflicts are handled:

| Situation | Default | With `--overwrite` |
| --- | --- | --- |
| Property doesn't exist yet | Added | Added |
| Property exists with the same value | No change | No change |
| Property exists with a different value | Existing value kept, warning shown | Replaced with the config value |

If the dimension doesn't exist in the org, the host is reported as
`NOT_FOUND` and skipped. The script never creates dimensions that don't exist.

## Requirements

- Python 3.9 or newer
- The `requests` library (`pip install -r requirements.txt`)
- A Splunk Observability Cloud API token that is allowed to read and modify
  metadata, for the same realm as the `realm` in your config

## Setup

```bash
cd ~/Projects/splunk-muting-properties
pip install -r requirements.txt

cp config.example.json config.json   # then edit it
cp hosts.example.csv hosts.csv       # then edit it
```

Provide the API token through an environment variable. It is deliberately not
stored in the config file:

```bash
export SFX_TOKEN='<your API token>'
```

`config.json` and `hosts.csv` are listed in `.gitignore` so they aren't
committed by accident.

## Config file reference

`config.json`:

```json
{
  "realm": "eu0",
  "dimension_key": "host.name",
  "token_env": "SFX_TOKEN",
  "properties": {
    "mute_group": "demo-muting-rule"
  }
}
```

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `realm` | Yes, unless `api_url` is set | | Your Splunk Observability Cloud realm, for example `eu0` or `us1`. The script uses `https://api.<realm>.signalfx.com`. |
| `api_url` | No | Built from `realm` | Full API base URL. Overrides `realm`. |
| `dimension_key` | No | `host` | The dimension that holds the values in your CSV. See [Finding the right dimension key and value](#finding-the-right-dimension-key-and-value). |
| `token_env` | No | `SFX_TOKEN` | The **name** of the environment variable that holds the token. It is not the token itself. |
| `properties` | Yes | | One or more `name: value` pairs to add to every host. Values can be strings or numbers; they're stored as strings. |

Property names can be up to 128 characters and values up to 256 characters.
The script checks both when it loads the config.

## Hosts CSV format

Put one hostname per row in the **first column**. Other columns are ignored.

```csv
hostname
ip-172-32-2-78.eu-west-1.compute.internal
ip-172-32-5-14.eu-west-1.compute.internal
```

- A header row is optional. It is skipped if the first cell is `host`,
  `hostname`, `host_name` or `name` (case-insensitive).
- Blank lines and lines starting with `#` are ignored.
- Duplicates are processed once.
- Values must match the dimension value **exactly**, including whether it is a
  short name or a fully qualified name.

## Usage

Always do a dry run first:

```bash
python3 add_properties.py --csv hosts.csv --dry-run
```

If the output looks right, apply the changes:

```bash
python3 add_properties.py --csv hosts.csv
```

To also replace properties that already exist with a different value:

```bash
python3 add_properties.py --csv hosts.csv --overwrite
```

### Removing the properties

To undo the tagging, run the script with `--remove`. It deletes only the
properties named in `config.json` from each host in the CSV. All other custom
properties, tags and the description are sent back unchanged, so they stay
active.

```bash
python3 add_properties.py --csv hosts.csv --remove --dry-run   # preview
python3 add_properties.py --csv hosts.csv --remove             # apply
```

By default a property is only removed if its current value matches the value in
the config (so you don't delete a `mute_group` that someone set to something
else). Add `--force` to remove the named properties whatever their value.
Hosts that don't have the property are reported as `UNCHANGED`.
`--remove` can't be combined with `--overwrite`.

How deletion works: the script sends the dimension back without the removed
properties and with everything else unchanged. After the write it re-reads the
host for up to about 10 seconds and prints a `WARNING` if a removed property is
still there. Check the result with a dry run, which reads the dimension again:
`BEFORE` should no longer list the property.
### Options

| Option | Description |
| --- | --- |
| `--csv FILE` | Required. CSV file with hostnames in the first column. |
| `--config FILE` | JSON config file. Default `config.json`. |
| `--dimension-key KEY` | Dimension the CSV values belong to, overriding `dimension_key` in the config (for example `host`). |
| `--dry-run` | Show what would change without modifying anything. Still sends a read-only `GET` for each host. |
| `--overwrite` | Replace existing properties that have a different value. |
| `--remove` | Remove the properties named in the config instead of adding them. Everything else is kept. |
| `--force` | With `--remove`, remove the named properties even if their value differs from the config. |
| `--quiet` | Hide the per-host BEFORE/AFTER property lists and print only the one-line result. |

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Every host was updated or already correct. |
| `1` | At least one host had an error or wasn't found, or the API rejected the token. |
| `2` | Bad config, missing CSV, missing token environment variable, or empty host list. |

## Reading the output

```
[DRY RUN] https://api.eu0.signalfx.com: 2 host(s), dimension key 'host.name', properties {"mute_group": "demo-muting-rule"}
WOULD_UPDATE  ip-172-32-2-78.eu-west-1.compute.internal - add mute_group='demo-muting-rule'
    BEFORE (1):
        aws_tag_Name = tf-demo-apache
    AFTER (proposed) (2):
        aws_tag_Name = tf-demo-apache
      + mute_group = demo-muting-rule

UNCHANGED     ip-172-32-5-14.eu-west-1.compute.internal - nothing to change
    BEFORE (2):
        aws_tag_Name = tf-demo-mysql
        mute_group = demo-muting-rule

Summary: unchanged=1, would_update=1
```

For every host that could be read, the script lists its custom properties
**before** the change and **after** it, so you can check that nothing
existing was lost:

- In a dry run, `AFTER (proposed)` is what would be written.
- In a real run, the script re-reads the host after the write and shows
  `AFTER (stored)`, which is what the API actually holds. If a property isn't
  visible yet, the result line includes a `WARNING`.
- `+` marks a property that is new and `~` marks one whose value changed.
- Use `--quiet` to hide these lists.

| Status | Meaning |
| --- | --- |
| `WOULD_UPDATE` | Dry run only. The host would be changed. |
| `UPDATED` | The properties were written. |
| `UNCHANGED` | The host already has the right values (or an existing different value was kept). |
| `NOT_FOUND` | No dimension exists with that key and value. Check the dimension key and the hostname. |
| `ERROR` | The read or write failed. The message includes the HTTP status. |

The script retries automatically on rate limiting (HTTP 429) and temporary
server errors (5xx). If the API returns 401 or 403 it stops straight away,
since every other host would fail the same way.

## Using the property in a muting rule

1. In Splunk Observability Cloud go to **Alerts > Muting rules** and create a
   rule.
2. Add a description. It is used as the rule's name.
3. In **Select properties**, choose your property (for example `mute_group`)
   and the value (for example `demo-muting-rule`).
4. Set the muting schedule, or mute indefinitely. Choose an end time or
   schedule if you only want the mute to last for a while.
5. Choose whether to clear existing alerts, and whether to send notifications
   for alerts that are still active when the mute ends.
6. Create the rule. It can take up to a minute to take effect.

Notes:

- Muting only stops **notifications**. Detectors still evaluate and alerts and
  events are still created and shown in the UI.
- When you add more than one detector or property to a rule, they're combined
  with AND.
- Test the rule before you rely on it. In particular, stop a test host and
  check under **Alerts > Active alerts** that its "no data" alerts are muted.
- A single muting rule value is limited to 256 characters, and the org limit is
  9,500 muting rules.

## Finding the right dimension key and value

The dimension key and value in your config and CSV have to match what your
hosts actually send. Setting a property on the wrong dimension succeeds
quietly but the muting rule won't match anything useful.

- For hosts monitored by the Splunk Distribution of the OpenTelemetry
  Collector, the host identity is commonly `host.name` (for EC2 this is the
  internal hostname, such as `ip-172-32-2-78.eu-west-1.compute.internal`). In
  some setups the collector maps it into the `host` dimension instead. Check
  the host's metadata in **Infrastructure** to see which dimensions it has.
- The EC2 `Name` tag (for example `tf-demo-apache`) is a custom property
  (`aws_tag_Name`), not a dimension. This script finds dimensions by key and
  value, so it can't look hosts up by their `Name` tag. Use the dimension
  value in the CSV.
- Splunk treats `host` and `host.name` as separate identifiers. If a host
  sends both, they can count as two hosts. Pick the one your detectors use.

You can check a single host without changing anything:

```bash
curl -s -H "X-SF-Token: $SFX_TOKEN" \
  "https://api.eu0.signalfx.com/v2/dimension/host.name/ip-172-32-2-78.eu-west-1.compute.internal"
```

A `200` response with JSON means the dimension exists. `404` means the key or
value doesn't match anything.

## Limits

- Up to 75 custom properties per dimension. The script reports an error
  instead of writing if a merge would go over this.
- Property names up to 128 characters, values up to 256 characters.
- Custom properties are single-valued.

## Troubleshooting

| Symptom | Likely cause and fix |
| --- | --- |
| `error: environment variable ... is not set` | `token_env` in the config must be the **name** of an environment variable (for example `SFX_TOKEN`), and that variable must be exported in your shell. Don't put the token itself in `token_env` or anywhere else in the config. |
| `fatal: HTTP 401 ...` | The token is invalid or belongs to a different realm. Check `SFX_TOKEN` and the `realm` setting. |
| `fatal: HTTP 403 ...` | The token is valid but isn't allowed to read or modify metadata. Use a token with the right permissions. |
| Every host is `NOT_FOUND` | Wrong `dimension_key`, or the CSV has names that aren't the dimension value (for example Name tags instead of `host.name` values). See [Finding the right dimension key and value](#finding-the-right-dimension-key-and-value). |
| A property wasn't changed even though the value is different | The existing value is kept unless you pass `--overwrite`. |
| Rule doesn't mute an alert | Check the property and value on the host, that the rule has no extra AND conditions, and allow up to a minute for the rule to take effect. |

## Files

| File | Purpose |
| --- | --- |
| `add_properties.py` | The script. |
| `config.example.json` | Example config. Copy to `config.json`. |
| `hosts.example.csv` | Example host list. Copy to `hosts.csv`. |
| `requirements.txt` | Python dependency (`requests`). |
