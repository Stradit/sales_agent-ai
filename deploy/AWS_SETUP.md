# Phase 3 — AWS setup (us-east-1, N. Virginia)

Console steps, in order. Each step lists what to write down — Phase 4 (GitHub Actions) needs
those values. Region selector top-right: **US East (N. Virginia) us-east-1** for every step.

Rough monthly cost (on-demand): EC2 t3.medium ≈ $30 · RDS db.t4g.small ≈ $24 + storage ·
EBS 40 GB ≈ $3 · ECR a few cents. Check the AWS pricing pages for current figures.

| Write down | Where it comes from |
|---|---|
| `AWS_ACCOUNT_ID` | Top-right account menu |
| `RDS_ENDPOINT`, DB master password | Step 2 |
| `ECR_REPOSITORY_URI` | Step 3 (`<account>.dkr.ecr.us-east-1.amazonaws.com/sales-ai`) |
| `EC2_HOST` (Elastic IP) | Step 5 |
| `AWS_DEPLOY_ROLE_ARN` | Step 7 |

---

## Step 1 — Security groups
EC2 → Security Groups → **Create security group**, default VPC, twice:

**`salesai-ec2-sg`** — inbound:

| Type | Port | Source | Why |
|---|---|---|---|
| HTTP | 80 | 0.0.0.0/0 and ::/0 | Certificate renewal + redirect to HTTPS |
| HTTPS | 443 | 0.0.0.0/0 and ::/0 | The app |
| SSH | 22 | 0.0.0.0/0 | GitHub Actions deploys over SSH (runners have no fixed IPs). Key-only login; password login is off on Ubuntu AMIs. Moving to SSM later lets you remove this rule. |

**`salesai-rds-sg`** — inbound: PostgreSQL 5432, source = **`salesai-ec2-sg`** (pick the group, not an IP).

## Step 2 — RDS PostgreSQL 18
RDS → **Create database** → Standard create:
- Engine **PostgreSQL**, version **18.x** (latest 18). Today's database is PostgreSQL 18.4, and an 18 dump
  doesn't reliably restore into an older version — if the console doesn't offer 18, stop and tell me.
- Template **Production** → then change: Single-AZ (Multi-AZ doubles the cost; add later if needed)
- DB instance identifier `salesai-db` · Master username `saadmin` · **Self managed** password (save it in your password manager)
- Instance class **db.t4g.small** (burstable)
- Storage **gp3, 20 GB**, storage autoscaling **on**, max **100 GB**
- Connectivity: default VPC · **Public access: No** · VPC security group **`salesai-rds-sg`** only (remove `default`)
- Additional configuration: Initial database name **`sales_ai`** · Backup retention **7 days** · Encryption **on** · **Deletion protection on**
- Create. When it's *Available*, copy the **Endpoint** → `RDS_ENDPOINT`.

`pg_trgm` (the only extension the app needs) is allowed on RDS; the app creates it on first start.

## Step 3 — ECR repository
ECR → Private registry → **Create repository**: name **`sales-ai`**, tag immutability **Immutable**, scan on push **on**.
Then the repository → Lifecycle policy → rule: *Image count more than 15 → expire*.
Copy the URI → `ECR_REPOSITORY_URI`.

## Step 4 — EC2 instance role (pulls images from ECR)
IAM → Roles → **Create role** → Trusted entity **AWS service: EC2** → attach:
- `AmazonEC2ContainerRegistryReadOnly`
- `AmazonSSMManagedInstanceCore` (gives you a browser shell via Session Manager as a backup to SSH)

Name it **`salesai-ec2-role`**.

## Step 5 — EC2 server
EC2 → **Launch instance**:
- Name `salesai-web` · AMI **Ubuntu Server 24.04 LTS (x86_64)** · Type **t3.medium**
- Key pair: create **`salesai-admin`** (ED25519, .pem) — this is YOUR login, keep it safe
- Network: default VPC, auto-assign public IP on, security group **`salesai-ec2-sg`**
- Storage: **40 GB gp3**
- Advanced details → IAM instance profile **`salesai-ec2-role`** · Metadata version **V2 only** · Metadata hop limit **2**
- Launch. Then EC2 → Elastic IPs → **Allocate** → **Associate** with `salesai-web`. Copy it → `EC2_HOST`.

## Step 6 — DNS
At your DNS provider: **A record** `<your-domain>` → `EC2_HOST`. Check from your PC:
`nslookup <your-domain>` shows the Elastic IP before Step 9.

## Step 7 — GitHub → AWS login (OIDC, no stored AWS keys)
1. IAM → Identity providers → **Add provider** → OpenID Connect · URL `https://token.actions.githubusercontent.com` · Audience `sts.amazonaws.com`.
2. IAM → Roles → **Create role** → **Web identity** → that provider, audience `sts.amazonaws.com`, GitHub organization **`Stradit`**, repository **`sales_agent-ai`**. Name **`salesai-github-deploy`**.
3. Edit its **trust policy** so only the `production` environment can use it:
```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "Federated": "arn:aws:iam::<AWS_ACCOUNT_ID>:oidc-provider/token.actions.githubusercontent.com" },
    "Action": "sts:AssumeRoleWithWebIdentity",
    "Condition": {
      "StringEquals": {
        "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
        "token.actions.githubusercontent.com:sub": "repo:Stradit/sales_agent-ai:environment:production"
      }
    }
  }]
}
```
4. Add an **inline policy** (push to the one repository only):
```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Effect": "Allow", "Action": "ecr:GetAuthorizationToken", "Resource": "*" },
    { "Effect": "Allow",
      "Action": ["ecr:BatchCheckLayerAvailability", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart",
                 "ecr:CompleteLayerUpload", "ecr:PutImage", "ecr:BatchGetImage", "ecr:DescribeImages"],
      "Resource": "arn:aws:ecr:us-east-1:<AWS_ACCOUNT_ID>:repository/sales-ai" }
  ]
}
```
Copy the role ARN → `AWS_DEPLOY_ROLE_ARN`.

## Step 8 — Deploy SSH key (GitHub → server)
On your PC (Git Bash):
```bash
ssh-keygen -t ed25519 -N "" -C "github-deploy" -f ~/.ssh/salesai_deploy
```
`~/.ssh/salesai_deploy.pub` goes on the server (Step 9); the private key `~/.ssh/salesai_deploy`
becomes a GitHub secret in Phase 4. Don't email or paste it anywhere else.

## Step 9 — Prepare the server
From the repo folder on your PC:
```bash
scp -i salesai-admin.pem -r deploy ubuntu@<EC2_HOST>:/tmp/
ssh -i salesai-admin.pem ubuntu@<EC2_HOST>
sudo bash /tmp/deploy/setup_server.sh <your-domain> <your-email>
sudo cp /tmp/deploy/docker-compose.prod.yml /tmp/deploy/remote_deploy.sh /opt/salesai/
sudo chown deploy:deploy /opt/salesai/docker-compose.prod.yml /opt/salesai/remote_deploy.sh
sudo tee -a /home/deploy/.ssh/authorized_keys < /dev/stdin   # paste salesai_deploy.pub, Enter, Ctrl+D
```
Checks on the server:
```bash
docker --version && aws --version && nginx -t
aws ecr describe-repositories --region us-east-1 --repository-names sales-ai   # proves the instance role works
sudo docker run --rm -it postgres:18 psql "postgresql://saadmin@<RDS_ENDPOINT>:5432/sales_ai?sslmode=require" -c 'select version()'   # proves EC2 → RDS (asks for the password)
curl -sI https://<your-domain> | head -1    # 502 is expected until the first deploy
```
From your PC: `ssh -i ~/.ssh/salesai_deploy deploy@<EC2_HOST> docker ps` must work (that's how GitHub will log in).

---
When all checks pass, send me the five values from the table at the top (no passwords or keys)
and we start Phase 4.

---

# Phase 4 — GitHub configuration

Workflow: `.github/workflows/deploy.yml` (manual: **Actions → Deploy to production → Run workflow**).
It runs the tests, builds the image, pushes it to ECR as `sales-ai:<commit>`, uploads the compose
file / deploy script / `.env` to the server, runs `remote_deploy.sh` (automatic rollback if the new
version isn't healthy) and finally checks `https://<your-domain>/api/health`.

> GitHub only shows the **Run workflow** button for workflows that exist on the repository's
> **default branch**. Merge this branch first, or run it from the CLI:
> `gh workflow run deploy.yml --repo Stradit/sales_agent-ai --ref feature/crm-core-phase1`

GitHub → repository **Settings → Environments → New environment `production`**:
- **Required reviewers:** yourself (every deploy waits for an approval click)
- **Deployment branches:** *Selected branches* → your release branch(es)

**Environment variables** (not secret):

| Name | Value |
|---|---|
| `AWS_DEPLOY_ROLE_ARN` | `arn:aws:iam::<AWS_ACCOUNT_ID>:role/salesai-github-deploy` |
| `ECR_REPOSITORY_URI` | `<AWS_ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com/sales-ai` |
| `EC2_HOST` | the Elastic IP |
| `SITE_URL` | `https://<your-domain>` |

**Environment secrets:**

| Name | Value |
|---|---|
| `DEPLOY_SSH_KEY` | contents of `~/.ssh/salesai_deploy` (the private key from Step 8) |
| `EC2_KNOWN_HOSTS` | output of `ssh-keyscan -t ed25519 <EC2_HOST>` run from your PC (pins the server's identity) |
| `ENV_FILE` | the whole production `.env`, filled in from `deploy/.env.production.example` |

The `.env` is written to `/opt/salesai/.env` (mode 600) on every deploy, so change settings by
editing the `ENV_FILE` secret and re-running the workflow.

---

# Phase 5 — Copy the database and first deploy

Production reads only `DATABASE_URL` (RDS). The source is today's local PostgreSQL 18.4.

1. **Freeze writes:** stop the local app (and anything else using the local DB).
2. **Dump** (this PC, Git Bash, repo root): `bash deploy/dump_local_db.sh`
   → `salesai_<date>.dump` + `salesai_<date>.counts.txt` (exact row counts per table).
3. **Upload:** `scp -i salesai-admin.pem salesai_<date>.dump salesai_<date>.counts.txt deploy/restore_db.sh ubuntu@<EC2_HOST>:/tmp/`
4. **Put the production `.env` on the server once** (the deploy workflow rewrites it later):
   `sudo -u deploy tee /opt/salesai/.env >/dev/null` → paste → Ctrl+D → `sudo chmod 600 /opt/salesai/.env`
5. **Restore** (EC2, before any deploy): `sudo bash /tmp/restore_db.sh /tmp/salesai_<date>.dump`
   → refuses a non-empty database; ends with "All N tables match row for row".
6. **Deploy:** GitHub → Actions → *Deploy to production* → Run workflow → approve.
7. **Copilot index:** sign in as admin → `/copilot-pipeline` → **Full re-embed**. (The vector store
   lives on disk, not in Postgres; the copied ledger says "indexed", so a normal sync wouldn't refill it.)
8. Delete the dump files from your PC and `/tmp` on the server — they contain all customer data.
