# Proxy UI + Graph Explorer against your AWS account

`nx-neptune-proxy-infra.json` creates everything the proxy UI
(`proxy/`, started with `make up`) needs in your AWS account, with least privilege
and encryption by default:

- the IAM role the proxy and Graph Explorer containers run as — also the Neptune
  Analytics import role;
- a KMS-encrypted, versioned staging bucket for projection CSVs and Athena results;
- an Athena workgroup with a per-query scan limit;
- optionally: read access to your source data, Lake Formation grants, and a
  synthetic sample dataset for a first run.

Graphs are not part of the stack: the proxy creates one per projection.

## Runbook

For your own AWS account. You need only the AWS CLI and a named profile you sign
in with — IAM Identity Center (SSO), a federated IAM role, or an IAM user. That
identity is your **operator principal**. Use a **non-production** account.

### Prerequisites

| Need | Version / note |
|---|---|
| This repository | `git clone https://github.com/awslabs/nx-neptune.git && cd nx-neptune` — run every command from the repository root |
| AWS CLI v2 | Recent enough to have `aws configure export-credentials` (`aws configure export-credentials help`) |
| Container runtime | Docker Desktop, podman 5.x (with a compose provider: `docker-compose` or `podman-compose`), or finch. On Apple Silicon, Graph Explorer runs as `linux/amd64` under emulation (Docker Desktop: enable *Use Rosetta*) |
| `make`, `bash` | GNU Make (macOS: Xcode Command Line Tools) |
| Node.js | `^20.19` or `>=22.12` (the UI's Vite build), with npm |
| Python | 3.9+ (sample-data generator, a few one-liners) |
| Region | One where Neptune Analytics is available — see the Regions list in [Limits for Neptune Analytics](https://docs.aws.amazon.com/neptune-analytics/latest/userguide/analytics-limits.html) |
| Permissions | To deploy: an administrator, or the [deployer policy](#deployer-permissions-optional). To use: being the operator principal (step 1) |

Keep the non-secret settings in one file and load it in every shell you use:

```bash
cat > ~/.nx-proxy.env <<'EOF'
export AWS_PROFILE=<the profile you sign in with>   # e.g. after `aws sso login --profile <name>`
export REGION=<your region>                         # e.g. eu-west-1
export AWS_DEFAULT_REGION=$REGION AWS_REGION=$REGION   # the CLI prefers AWS_REGION, boto3 AWS_DEFAULT_REGION
export APP=nx-neptune                               # ApplicationId: prefix of every resource name
export RT=docker                                    # or podman / finch
EOF
. ~/.nx-proxy.env
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
aws sts get-caller-identity --query Arn --output text   # your operator principal, see step 1
$RT info >/dev/null && echo "$RT is running"
```

If the last command fails, start the runtime first: Docker Desktop, `podman machine start`
(macOS/Windows), or `finch vm start`.

### Cost

You pay for what runs, at standard prices:

- **Neptune Analytics** — each graph the proxy creates is billed per hour for its
  provisioned memory (the proxy creates 16 GB graphs by default) until you delete it.
  This is the main cost. See [Neptune pricing](https://aws.amazon.com/neptune/pricing/).
- **KMS** — one customer-managed key (monthly fee plus requests). It is retained when
  the stack is deleted until you schedule its deletion (step 7).
- **Athena** — per data scanned (capped at 10 GB per query by the workgroup); **S3** —
  storage and requests for the staging bucket.

Delete graphs as soon as you're done (Graphs page in the proxy UI).

### Deployer permissions (optional)

An administrator can deploy the proxy stack directly — skip this section.

To let a non-administrator deploy, an administrator deploys a small bootstrap stack
once per account and Region:

```bash
aws cloudformation deploy --region "$REGION" --stack-name "$APP-proxy-deployer" \
  --template-file cloudformation-templates/nx-neptune-proxy-deployer.json \
  --capabilities CAPABILITY_NAMED_IAM --parameter-overrides ApplicationId="$APP"
#   add the same GlueDatabaseName / SourceDataBucket / SourceDataPrefix / SourceDataKmsKeyArn as the proxy stack
aws cloudformation describe-stacks --region "$REGION" --stack-name "$APP-proxy-deployer" --query 'Stacks[0].Outputs' --output table
```

It creates:

- `ExecutionRoleArn` — a CloudFormation execution role that creates the proxy stack's
  resources. It can create or change the proxy role only with the boundary below.
- `ProxyBoundaryArn` — the permissions boundary the proxy role must carry.
- `DeployerPolicyArn` — attach it to the deployer (an IAM role or user, or to an IAM
  Identity Center permission set as a customer managed policy). It can create, update
  and delete the proxy stack **only through the execution role**, upload the sample,
  and tear down the retained bucket and key.

Pass the same data parameters you will give the proxy stack (`GlueDatabaseName`,
`SourceDataBucket`, `SourceDataPrefix`, `SourceDataKmsKeyArn`, all optional): the
boundary admits only what they name. Update the bootstrap stack when they change.

- With Lake Formation enforced, also grant the execution role `CREATE_DATABASE` (for the
  sample) and grantable permissions on any database you want the proxy to read.
- The bootstrap path always uses `ProxyBoundaryArn`; an organization-required boundary of
  your own can't be combined with it — deploy as an administrator in that case.
- The deployer policy doesn't include `sts:AssumeRole` on the proxy role. A deployer who
  is also the operator needs it from their own permissions (step 1).
- `lakeformation:ListPermissions` (the check in step 1) can return partial results for a
  principal that isn't a Lake Formation administrator.

### 1. Deploy the stack

**Operator principal.** Only principals matching `OperatorPrincipalArn` can assume
the proxy role. It is matched with `aws:PrincipalArn` (wildcards allowed), so it keeps
working when your sign-in role is recreated:

| You sign in with | `OperatorPrincipalArn` |
|---|---|
| IAM Identity Center | `arn:aws:iam::<account>:role/aws-reserved/sso.amazonaws.com/*AWSReservedSSO_<PermissionSetName>_*` (the `*` covers the optional Region path segment) |
| An IAM role | the role's full ARN, e.g. `arn:aws:iam::<account>:role/Admin` |
| An IAM user | the user's ARN, e.g. `arn:aws:iam::<account>:user/alice` |

`get-caller-identity` prints an *assumed-role* session ARN
(`arn:aws:sts::<account>:assumed-role/<RoleName>/<session>`) without the role's path.
Get the full role ARN with `aws iam get-role --role-name <RoleName> --query Role.Arn --output text`.
Unless you are an administrator, your identity also needs `sts:AssumeRole` on
`arn:aws:iam::<account>:role/$APP-proxy-*`.

**Existing graphs.** The proxy role may be able to take over any graph in the Region
(see [Known limitations](#known-limitations)). List them first; if any must not be
touched, choose another Region or account:

```bash
aws neptune-graph list-graphs --region "$REGION" --query 'graphs[].[name,id]' --output text
```

**Lake Formation.** Check whether it governs the Data Catalog:

```bash
aws lakeformation get-data-lake-settings --region "$REGION" --query 'DataLakeSettings.CreateDatabaseDefaultPermissions'
# Using your own database? Also check that one:
aws lakeformation list-permissions --region "$REGION" --resource '{"Database":{"Name":"<db>"}}' \
  --query 'PrincipalResourcePermissions[].Principal.DataLakePrincipalIdentifier'
```

If both list `IAM_ALLOWED_PRINCIPALS`, Lake Formation is not enforcing permissions:
keep `GrantLakeFormation=false`. Otherwise set `GrantLakeFormation=true` and deploy as
a Lake Formation administrator (or a principal with `CREATE_DATABASE` and grantable
permissions); without the grants, databases stay hidden in the Import page.

```bash
OPERATOR_PRINCIPAL_ARN='<value from the table above>'

aws cloudformation deploy --region "$REGION" \
  --stack-name "$APP-proxy" \
  --template-file cloudformation-templates/nx-neptune-proxy-infra.json \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides ApplicationId="$APP" OperatorPrincipalArn="$OPERATOR_PRINCIPAL_ARN" \
      CreateSampleDataset=true GrantLakeFormation=false \
  --tags application="$APP"
# Non-administrator (bootstrap stack): add  --role-arn <ExecutionRoleArn>
#   and the parameter  PermissionsBoundaryArn=<ProxyBoundaryArn>
# Organization-required boundary of your own: PermissionsBoundaryArn=<policy ARN>
# Your own data as well: GlueDatabaseName=<db> SourceDataBucket=<bucket> SourceDataPrefix=<prefix>/
#   (and SourceDataKmsKeyArn=<key ARN> if that bucket uses SSE-KMS)

aws cloudformation update-termination-protection --enable-termination-protection \
  --stack-name "$APP-proxy" --region "$REGION"
aws cloudformation describe-stacks --stack-name "$APP-proxy" --region "$REGION" \
  --query 'Stacks[0].Outputs' --output table
```

### 1b. Load the sample data (with `CreateSampleDataset=true`)

The stack creates the Glue database `nx_neptune_sample` and the table
`transactions_sample` over `s3://<staging bucket>/sample/transactions/`. Generate the
rows and upload them with your own credentials:

```bash
python3 cloudformation-templates/sample-data/generate_transactions.py --out /tmp/transactions.csv
# {"rows": 10000, "expected_graph": {"nodes": 3981, "edges": 10000}, ...}
aws s3 cp /tmp/transactions.csv "s3://$APP-proxy-$ACCOUNT_ID-$REGION/sample/transactions/transactions.csv" --only-show-errors
```

Like everything in the bucket, the sample expires after `StagingRetentionDays`
(default 7); the table then returns no rows — run the two commands again.

The data is synthetic: seeded random values in a column layout modelled on
PaySim (E. A. Lopez-Rojas, A. Elmir, S. Axelsson, "PaySim: A financial mobile money
simulator for fraud detection", EMSS 2016; the
[PaySim dataset](https://www.kaggle.com/datasets/ealaxi/paysim1) is CC BY-SA 4.0).
No PaySim data is included or downloaded. Because the layout matches, the
`CREATE PROPERTY GRAPH` statement from `notebooks/create_property_graph_pgq.ipynb` works on
it with the table name changed (see step 3).

### 2. Get short-lived credentials for the proxy role

Add a profile that assumes the proxy role from your sign-in profile:

```ini
# ~/.aws/config
[profile nx-proxy]
role_arn = <ProxyRoleArn output>
source_profile = <the profile you sign in with>
role_session_name = nx-proxy
region = <your region>
```

Sessions last at most one hour: assuming a role from another role's session is
*role chaining*, which AWS caps at one hour.

```bash
cat >> ~/.nx-proxy.env <<EOF
export NETWORKX_ATHENA_WORKGROUP=<AthenaWorkGroupName output>
EOF
$RT pull --platform linux/amd64 public.ecr.aws/neptune/graph-explorer:latest
echo "export GRAPH_EXPLORER_IMAGE=$($RT image inspect --format '{{index .RepoDigests 0}}' public.ecr.aws/neptune/graph-explorer:latest)" >> ~/.nx-proxy.env
. ~/.nx-proxy.env

eval "$(aws configure export-credentials --profile nx-proxy --format env)"
# Print only the expiry: --query is ignored by this command, so never print its full output.
aws configure export-credentials --profile nx-proxy --format process \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["Expiration"])'
aws sts get-caller-identity --query Arn --output text    # must show assumed-role/<APP>-proxy-<region>/nx-proxy
```

### 3. Start

```bash
make -C proxy ui up RUNTIME=$RT                 # fails if AWS_DEFAULT_REGION is unset or compose fails
$RT compose -f proxy/docker-compose.yml ps      # proxy "healthy", graph-explorer "running"
$RT compose -f proxy/docker-compose.yml logs proxy | grep -m1 -o 'http://127.0.0.1:8080/?token=[^ ]*'
```

Open the launch URL; its token is valid for this container run only and is kept
in memory, so after a page refresh reopen the launch URL. Open Graph Explorer at
`https://localhost:8443/explorer` (the UI's "Open in Graph Explorer" links point
there) and accept its self-signed certificate once.

In the Import page, choose the `StagingBucketUri` output as the staging bucket and,
for the sample, the database `nx_neptune_sample`. Use this PGQ statement for the
sample; it gives 3,981 vertices and 10,000 edges:

```sql
CREATE PROPERTY GRAPH financial
  VERTEX TABLES (
    transactions_sample AS sender    KEY (nameOrig) LABEL customer NO PROPERTIES,
    transactions_sample AS recipient KEY (nameDest) LABEL customer NO PROPERTIES
  )
  EDGE TABLES (
    transactions_sample
      SOURCE KEY (nameOrig) REFERENCES sender
      DESTINATION KEY (nameDest) REFERENCES recipient
      LABEL transfer PROPERTIES ALL COLUMNS
  )
```

Then query the graph in Graph Explorer (openCypher). Customers have no properties —
the account name is the vertex ID; transfer properties are lowercase (the Glue column
names). Queries that return `p` draw on the canvas; the others return a table.

```cypher
// Fraudulent transfers and the accounts involved
MATCH p = (:customer)-[t:transfer]->(:customer) WHERE t.isfraud = 1 RETURN p LIMIT 50

// Recipients of fraud from more than one sender (possible mules)
MATCH (s:customer)-[t:transfer]->(r:customer) WHERE t.isfraud = 1
WITH r, count(DISTINCT s) AS senders WHERE senders > 1
RETURN id(r) AS recipient, senders ORDER BY senders DESC

// Two-hop neighbourhood of one account
MATCH p = (c:customer {`~id`: 'C1000000001'})-[:transfer*1..2]-() RETURN p LIMIT 100

// Largest TRANSFER transactions
MATCH p = ()-[t:transfer]->() WHERE t.type = 'TRANSFER' AND t.amount > 150000
RETURN p ORDER BY t.amount DESC LIMIT 25

// Most active senders
MATCH (c:customer)-[t:transfer]->()
RETURN id(c) AS customer, count(t) AS transfers, round(sum(t.amount)) AS total
ORDER BY transfers DESC LIMIT 10

// Transfers and fraud by transaction type
MATCH ()-[t:transfer]->()
RETURN t.type AS type, count(*) AS transfers, sum(t.isfraud) AS fraud ORDER BY transfers DESC
```

`make up` turns graph deletion protection **off** for convenience. For anything
longer-lived, run compose directly — after `. ~/.nx-proxy.env`, since that path skips
`make`'s region check:
`NETWORKX_PUBLIC_CONNECTIVITY=true NETWORKX_DELETION_PROTECTION=true $RT compose -f proxy/docker-compose.yml up --build -d`.

### 4. Verify

```bash
curl -fsS http://127.0.0.1:8080/health
aws neptune-graph list-graphs --region "$REGION" --query "graphs[].{name:name,status:status}"   # after the first Execute
```

### 5. Refresh credentials (before the hour is up)

```bash
. ~/.nx-proxy.env
# Drop the CLI's cached session so you get a new one, not the cached (possibly expiring) one.
find ~/.aws/cli/cache -name '*.json' -exec grep -l "assumed-role/$APP-proxy-" {} + 2>/dev/null | xargs rm -f
eval "$(aws configure export-credentials --profile nx-proxy --format env)"
make -C proxy up RUNTIME=$RT
```

Projects and projections persist (named volume). Reopen the new launch URL.

### 6. Retry a failed projection

Clear its staging prefix first, or the retry imports stale CSVs a second time
(never run it with an empty `<projection-id>`):

```bash
aws s3 rm "s3://$APP-proxy-$ACCOUNT_ID-$REGION/<projection-id>/" --recursive
```

### 7. Tear down

1. **First, while the proxy's credentials are valid** (refresh them if needed, step 5),
   delete graphs through the proxy UI (Graphs page). Once the stack is deleted its role
   is gone; leftover graphs (`nxp-*`, tag `agent=nx-neptune`) must then be deleted with
   your own credentials: `aws neptune-graph delete-graph --region "$REGION" --graph-identifier <id> --skip-snapshot`.
2. `$RT compose -f proxy/docker-compose.yml down -v` (removes the projects volume).
3. Delete the stack with your own credentials:

   ```bash
   unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN; . ~/.nx-proxy.env
   ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
   KEY_ARN=$(aws cloudformation describe-stacks --region "$REGION" --stack-name "$APP-proxy" \
     --query "Stacks[0].Outputs[?OutputKey=='StagingKeyArn'].OutputValue" --output text)   # the key is retained; its alias is not
   aws cloudformation update-termination-protection --region "$REGION" --no-enable-termination-protection --stack-name "$APP-proxy"
   aws cloudformation delete-stack --region "$REGION" --stack-name "$APP-proxy"      # non-admin: add --role-arn <ExecutionRoleArn>
   aws cloudformation wait stack-delete-complete --region "$REGION" --stack-name "$APP-proxy"
   ```
4. The bucket and the key are retained. A versioned bucket must be emptied of every
   object version and delete marker before it can be deleted:

   ```bash
   BUCKET="$APP-proxy-$ACCOUNT_ID-$REGION"
   while :; do
     BATCH=$(aws s3api list-object-versions --bucket "$BUCKET" --max-items 500 --output json \
       --query '{Objects: [Versions, DeleteMarkers][][].{Key: Key, VersionId: VersionId}, Quiet: `true`}')
     [ "$(echo "$BATCH" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["Objects"] or []))')" = 0 ] && break
     ERRORS=$(aws s3api delete-objects --bucket "$BUCKET" --delete "$BATCH" --query 'length(Errors || `[]`)' --output text)
     [ "$ERRORS" = 0 ] || { echo "delete-objects reported $ERRORS error(s); stopping" >&2; break; }
   done
   aws s3api delete-bucket --bucket "$BUCKET"
   aws kms schedule-key-deletion --region "$REGION" --key-id "$KEY_ARN" --pending-window-in-days 7
   ```
5. Clean up locally: remove the `nx-proxy` profile from `~/.aws/config`, the cached
   session (`find ~/.aws/cli/cache -name '*.json' -exec grep -l "assumed-role/$APP-proxy-" {} + | xargs rm -f`)
   and `~/.nx-proxy.env`.
6. If you deployed the bootstrap stack and no longer need it: detach `DeployerPolicyArn`
   from its users, then `aws cloudformation delete-stack --region "$REGION" --stack-name "$APP-proxy-deployer"`.


## Security properties

Following the AWS security best practices for IAM, S3, KMS and Neptune Analytics:

- **Least privilege.**
  - Graph changes (reset, delete, update, start, stop, queries) are granted only on graphs tagged `agent=nx-neptune`, the tag the library adds to every graph it creates. Validated: reset and query of an untagged graph are denied. This does **not** yet stop the role from adding that tag to an existing graph — see [Known limitations](#known-limitations).
  - `CreateGraph` requires that tag on the request. The role has no snapshot permissions (the proxy doesn't use snapshots).
  - `iam:PassRole` is limited to the role itself and to Neptune Analytics.
  - S3, KMS, Athena and Glue access is limited to the stack's bucket, key and workgroup, your database (if set) and the sample database (if created). Source data is read-only and listing is limited to its prefix.
  - `"*"` resources are used only where the service offers no resource-level scoping (`List*` calls, the UI's bucket/catalog pickers, `lakeformation:GetDataAccess`) and in the temporary preflight compatibility policy, which is region-limited and holds only `CreateGraph`, `TagResource` and two Athena query actions.
- **Who can use it.** Only principals in this account whose ARN matches `OperatorPrincipalArn` (`aws:PrincipalArn`) can assume the role. Sessions last at most one hour.
- **Organization guardrails.** `PermissionsBoundaryArn` sets a permissions boundary on the role. Non-administrators deploy through the optional bootstrap stack (`nx-neptune-proxy-deployer.json`): a CloudFormation execution role (named outside the `<app>-proxy-*` namespace it manages) that can create the proxy role only with the boundary it also creates, and a deployer policy that can run the stack only through that role, only with this stack's resource types, and never as a resource import. The boundary repeats the proxy role's own resource scoping (the stack's bucket, the stack-tagged key, the workgroup, tagged graphs, the named source data), so a role created through this path can't reach anything else.
- **Service trust.** The Neptune Analytics service trust uses `aws:SourceAccount` and `aws:SourceArn` with `…IfExists`: requests that carry the keys must match this account and its Neptune Analytics resources. The assume check Neptune Analytics makes at `StartImportTask` carries neither key, so that check is not narrowed (see [Trust policy](#trust-policy-measured)); a plain condition would break every import.
- **Encryption.**
  - The bucket and the Athena results use a customer-managed KMS key with rotation on; the key policy delegates to IAM.
  - Bucket keys reduce KMS calls.
  - The bucket policy denies non-TLS access and TLS older than 1.2.
- **No public data.**
  - Public access is fully blocked and ACLs are disabled (`BucketOwnerEnforced`).
  - Graphs get a public endpoint so Graph Explorer can reach them from your machine, but every request still needs SigV4 IAM auth.
- **Data lifecycle.**
  - Versioning is on; the library requires it.
  - Old versions expire after one day.
  - Everything in the bucket — staged CSVs, query results, project exports and the sample data — expires after `StagingRetentionDays` (default 7).
  - Unfinished multipart uploads are removed after one day.
- **Retained on delete.** The bucket and the key are kept when the stack is deleted, so deleting the stack can't lose data or make it unreadable. Their deletion is a separate, explicit teardown step.
- **Local exposure.**
  - Both containers publish on `127.0.0.1` only.
  - Both run with `no-new-privileges`; the proxy also drops all capabilities.
  - The containers receive credentials through the environment and write none to disk. The AWS CLI itself caches the short-lived session under `~/.aws/cli/cache` (and SSO tokens under `~/.aws/sso/cache`); the runbook removes the session cache on refresh and at teardown.
  - Never print `aws configure export-credentials` output: `--query` is ignored by that command and the secret key and session token are printed in full. The runbook extracts only the expiry.

### Known limitations

- **Tagging an existing graph.** `TagResource` is granted when the request adds `agent=nx-neptune` (needed because `CreateGraph` tags the new graph). Neptune Analytics may also accept that request for an *existing* untagged graph, which would then fall under the role's tag-scoped permissions. Whether the create-time authorization exposes `aws:ResourceTag` — which would allow conditioning `TagResource` on it — has not been verified. Until it is, treat the role as able to take over graphs in the same account and Region, and don't deploy it in accounts holding graphs it must not touch.
- **Preflight compatibility** (`PreflightCompatibility=true`, default) grants `CreateGraph` and `TagResource` on any graph in the Region, which widens the item above. Set it to `false` once the library's preflight simulates real ARNs (issue 576).
- **finch** is untested: the named volume relies on the runtime copying the image's `/home/appuser` ownership into a new volume (verified on podman; Docker documents the same behaviour).
- **What a deployer decides.** Whoever deploys the stack chooses `OperatorPrincipalArn` — who may use the proxy role — and the staging bucket's configuration. With the bootstrap stack, a deployer could write a template that trusts a principal of their choosing or loosens the staging bucket's policy; the boundary still limits what that role can reach. Grant the deployer policy only to people you would also let operate the proxy.
- **One sample per account and Region**: the sample database name `nx_neptune_sample` is fixed.
- Commercial AWS partition only (`arn:aws`); not tested in China or GovCloud (Neptune Analytics is not offered in GovCloud).

### Trust policy (measured)

The `neptune-graph.amazonaws.com` trust statement was tested with a 2-node import
for each condition variant:

| Condition on the service trust | `StartImportTask` |
|---|---|
| none | succeeded |
| `StringEquals aws:SourceAccount` | rejected: `The S3 arn … is inaccessible or your role does not have access to it` |
| `ArnLike aws:SourceArn` = `arn:…:neptune-graph:<region>:<account>:*` | rejected (same error) |
| `ArnLike aws:SourceArn` = the graph's ARN | rejected (same error) |
| both of the above | rejected (same error) |
| `StringEqualsIfExists aws:SourceAccount` | succeeded |
| `ArnLikeIfExists aws:SourceArn` (account wildcard) | succeeded |
| **both, `…IfExists`** (used by the stack) | **succeeded** |

Neptune Analytics checks that it can assume the role when `StartImportTask` is
called, and that check carries neither key. A plain confused-deputy condition
therefore makes every import fail, and the error message blames S3 rather than the
trust policy. With `…IfExists`, the check passes, and any request that does carry
the keys must match this account and its Neptune Analytics resources.
