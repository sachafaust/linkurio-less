"""Seed a LOCAL test Neo4j with synthetic, Cartography-shaped data.

Everything here is fake: account ids, ARNs, names, IPs and e-mails are generated.
Labels, multi-labels, relationship types and property names follow Cartography's
data model (checked against cartography/models via its introspection API, Nov 2026).

Usage:
    NEO4J_URI=bolt://localhost:7687 NEO4J_USER=neo4j NEO4J_PASSWORD=... \
        python3 seed_cartography.py --wipe
    The same variables can live in a gitignored .env. The environment wins over that file.

Options: --fixtures loads the real-schema snapshot exported from Cartography's own demo seeds and
test fixtures (data/cartography-fixtures.json.gz); --scale N replicates the synthetic estate N times
and --hub N adds a supernode, for large-graph benchmarks.

--live-scale loads the minimum graph this explorer is meant to stay usable on:
15,000,000 nodes and 60,000,000 relationships, streamed in batches (nothing is held in memory).
--nodes and --edges override those totals. --dry-run prints the plan and does not connect.

--wipe deletes every node in the target database first. It refuses to run unless
NEO4J_URI points at localhost/127.0.0.1, so it cannot touch a remote database.
"""
from __future__ import annotations

import argparse
import os
import random
import sys
import time
from collections import defaultdict
from urllib.parse import urlparse

# The smallest graph the explorer is expected to stay usable on. The browser never
# loads it; the server returns a working set. This seeder builds that graph in batches.
LIVE_NODES = 15_000_000
LIVE_EDGES = 60_000_000
LIVE_ACCOUNTS = 1_500
LIVE_SUPER = 2_000_000  # resources owned by account 0, so expand hits a real hub
# Weights sum to LIVE_NODES - LIVE_ACCOUNTS. plan_live_scale scales them for other sizes.
LABEL_WEIGHTS = [
    ("AWSEC2Instance", ("AWSEC2Instance", "EC2Instance", "ComputeInstance"), 6_500_000),
    ("AWSS3Bucket", ("AWSS3Bucket",), 1_500_000),
    ("AWSRole", ("AWSRole", "AWSPrincipal"), 900_000),
    ("AWSPolicy", ("AWSPolicy",), 600_000),
    ("AWSVpc", ("AWSVpc",), 400_000),
    ("EC2SecurityGroup", ("EC2SecurityGroup",), 1_200_000),
    ("IpPermissionInbound", ("IpPermissionInbound",), 800_000),
    ("GitHubRepository", ("GitHubRepository", "CodeRepository"), 700_000),
    ("GitHubUser", ("GitHubUser", "UserAccount"), 748_500),
    ("GCPInstance", ("GCPInstance", "ComputeInstance", "Instance"), 600_000),
    ("GCPBucket", ("GCPBucket", "ObjectStorage"), 400_000),
    ("OktaUser", ("OktaUser", "UserAccount"), 650_000),
]
EXTRA_TYPES = ("STS_ASSUMEROLE_ALLOW", "MEMBER_OF", "POLICY", "TAGGED")
_STRIDE_CACHE = {}

from neo4j import GraphDatabase

R = random.Random(42)
NOW = int(time.time())

FIRST = "ada alan grace linus ken dennis barbara margaret john katherine tim vint radia whitfield hedy annie frances donald edsger niklaus guido bjarne james brendan rasmus yukihiro anders larry sergey susan".split()
LAST = "lovelace turing hopper torvalds thompson ritchie liskov hamilton backus johnson berners-lee cerf perlman diffie lamarr easley allen knuth dijkstra wirth rossum stroustrup gosling eich lerdorf matsumoto hejlsberg page brin wojcicki".split()
REGIONS = ["us-east-1", "us-west-2", "eu-west-1"]
WORDS = "payments ledger billing auth gateway search ingest etl metrics logs web api worker cache queue reports analytics ml export import notify mailer cdn assets backup audit".split()


class G:
    def __init__(self):
        self.nodes = defaultdict(list)  # labels tuple -> list of props
        self.rels = defaultdict(list)   # (src_label, type, dst_label) -> list of (src_id, dst_id, props)

    def node(self, labels, **props):
        props.setdefault("firstseen", NOW - R.randint(86400, 86400 * 400))
        props.setdefault("lastupdated", NOW - R.randint(0, 3600))
        self.nodes[tuple(labels)].append(props)
        return props

    def rel(self, src, typ, dst, src_label, dst_label, **props):
        props.setdefault("firstseen", NOW - R.randint(86400, 86400 * 300))
        props.setdefault("lastupdated", NOW - R.randint(0, 3600))
        self.rels[(src_label, typ, dst_label)].append((src["id"], dst["id"], props))


def person():
    f, l = R.choice(FIRST), R.choice(LAST)
    return f, l, f"{f}.{l}@example-corp.test"


def build() -> G:
    g = G()
    accounts = []
    for name in ["acme-prod", "acme-staging", "acme-dev"]:
        aid = str(R.randint(10**11, 10**12 - 1))
        a = g.node(["AWSAccount", "Tenant"], id=aid, name=name, inscope=True, foreign=False,
                   arn=f"arn:aws:iam::{aid}:root", email=f"aws+{name}@example-corp.test")
        accounts.append(a)
    # one foreign account that shows up via role trust
    fid = str(R.randint(10**11, 10**12 - 1))
    foreign = g.node(["AWSAccount", "Tenant"], id=fid, name=f"vendor-{fid[-4:]}", inscope=False, foreign=True,
                     arn=f"arn:aws:iam::{fid}:root")
    froot = g.node(["AWSPrincipal"], id=f"arn:aws:iam::{fid}:root", arn=f"arn:aws:iam::{fid}:root", type="AWS")
    g.rel(foreign, "RESOURCE", froot, "AWSAccount", "AWSPrincipal")

    gh_roles = []
    okta_roles = []
    for a in accounts:
        aid = a["id"]
        A = lambda n: g.rel(a, "RESOURCE", n[1], "AWSAccount", n[0]) or n[1]
        kms = [A(("AWSKMSKey", g.node(["AWSKMSKey", "KMSKey", "EncryptionKey"], id=f"{u}", arn=f"arn:aws:kms:{r}:{aid}:key/{u}",
                                          key_id=u, region=r, enabled=True, key_state="Enabled", key_manager=R.choice(["CUSTOMER", "AWS"]),
                                          description=f"{R.choice(WORDS)} data key", creation_date=NOW - R.randint(10**6, 10**8))))
               for r, u in ((R.choice(REGIONS), f"{R.getrandbits(64):016x}-{R.getrandbits(32):08x}") for _ in range(5))]
        # network
        sgs_all, subnets_all = [], []
        for region in REGIONS[:2]:
            for v in range(2):
                vid = f"vpc-{R.getrandbits(48):012x}"
                cidr = f"10.{R.randint(0, 255)}.0.0/16"
                vpc = A(("AWSVpc", g.node(["AWSVpc", "VirtualNetwork"], id=vid, vpcid=vid, region=region, primary_cidr_block=cidr,
                                          is_default=(v == 0 and region == "us-east-1"), state="available", instance_tenancy="default")))
                for s in range(3):
                    sid = f"subnet-{R.getrandbits(48):012x}"
                    pub = s == 0
                    sn = A(("AWSEC2Subnet", g.node(["AWSEC2Subnet", "EC2Subnet", "Subnet"], id=sid, subnetid=sid, subnet_id=sid, region=region,
                                                   availability_zone=f"{region}{'abc'[s]}", cidr_block=cidr.replace(".0.0/16", f".{s * 16}.0/20"),
                                                   map_public_ip_on_launch=pub, name=f"{'public' if pub else 'private'}-{region}{'abc'[s]}",
                                                   vpc_id=vid, state="available", available_ip_address_count=R.randint(50, 4000))))
                    g.rel(sn, "MEMBER_OF_AWS_VPC", vpc, "AWSEC2Subnet", "AWSVpc")
                    subnets_all.append((sn, pub))
                for k in range(5):
                    gid = f"sg-{R.getrandbits(48):012x}"
                    nm = ["default", "web-public", "app-internal", "db", "bastion"][k]
                    sg = A(("AWSEC2SecurityGroup", g.node(["AWSEC2SecurityGroup", "EC2SecurityGroup", "NetworkAccessControl"], id=gid, groupid=gid,
                                                          name=nm, description=f"{nm} security group", region=region)))
                    g.rel(sg, "MEMBER_OF_EC2_SECURITY_GROUP", vpc, "AWSEC2SecurityGroup", "AWSVpc")
                    sgs_all.append(sg)
                    for port in {"web-public": [443, 80], "bastion": [22], "db": [5432], "app-internal": [8080], "default": []}[nm]:
                        rid = f"{gid}/IpPermissionInbound/tcp{port}{port}"
                        rule = g.node(["AWSIpPermissionInbound", "IpPermissionInbound", "AWSIpRule", "IpRule"], id=rid, ruleid=rid,
                                      groupid=gid, protocol="tcp", fromport=port, toport=port)
                        g.rel(rule, "MEMBER_OF_EC2_SECURITY_GROUP", sg, "AWSIpPermissionInbound", "AWSEC2SecurityGroup")
                        rng = "0.0.0.0/0" if nm in ("web-public", "bastion") else cidr
                        ipr = g.node(["AWSIpRange", "IpRange"], id=f"{rng}|{rid}", range=rng)
                        g.rel(ipr, "MEMBER_OF_IP_RULE", rule, "AWSIpRange", "AWSIpPermissionInbound")
        # IAM
        policies = []
        for nm in ["AdministratorAccess", "ReadOnlyAccess", "AmazonS3FullAccess", "AmazonEC2ReadOnlyAccess", "SecurityAudit", "PowerUserAccess"]:
            p = g.node(["AWSManagedPolicy", "AWSPolicy"], id=f"arn:aws:iam::aws:policy/{nm}-{aid}", arn=f"arn:aws:iam::aws:policy/{nm}", name=nm, type="managed")
            policies.append(p)
            eff_actions = {"AdministratorAccess": ["*"], "ReadOnlyAccess": ["*:Get*", "*:List*", "*:Describe*"], "AmazonS3FullAccess": ["s3:*"],
                           "AmazonEC2ReadOnlyAccess": ["ec2:Describe*"], "SecurityAudit": ["iam:Get*", "iam:List*"], "PowerUserAccess": ["*"]}[nm]
            st = g.node(["AWSPolicyStatement"], id=f"{p['id']}/statement/1", effect="Allow", action=eff_actions, resource=["*"], sid="")
            g.rel(p, "STATEMENT", st, "AWSPolicy", "AWSPolicyStatement")
        groups = []
        for nm in ["admins", "developers", "readonly", "security", "billing"]:
            grp = A(("AWSGroup", g.node(["AWSGroup", "AWSPrincipal", "UserGroup"], id=f"arn:aws:iam::{aid}:group/{nm}", arn=f"arn:aws:iam::{aid}:group/{nm}",
                                        name=nm, groupid="AGPA" + "".join(R.choices("ABCDEFGHJKLMNPQRSTUVWXYZ234567", k=17)), path="/",
                                        createdate=NOW - R.randint(10**7, 10**8))))
            groups.append(grp)
            g.rel(grp, "POLICY", R.choice(policies), "AWSPrincipal", "AWSPolicy")
        users = []
        for _ in range(18):
            f, l, email = person()
            un = f"{f}.{l}"
            if any(u["name"] == un for u in users):
                continue
            u = A(("AWSUser", g.node(["AWSUser", "AWSPrincipal", "UserAccount"], id=f"arn:aws:iam::{aid}:user/{un}", arn=f"arn:aws:iam::{aid}:user/{un}",
                                     name=un, userid="AIDA" + "".join(R.choices("ABCDEFGHJKLMNPQRSTUVWXYZ234567", k=17)), path="/",
                                     createdate=NOW - R.randint(10**7, 10**8), passwordlastused=NOW - R.randint(0, 10**7))))
            users.append(u)
            for grp in R.sample(groups, R.randint(1, 2)):
                g.rel(u, "MEMBER_AWS_GROUP", grp, "AWSUser", "AWSGroup")
            if R.random() < 0.5:
                kid = "AKIA" + "".join(R.choices("ABCDEFGHJKLMNPQRSTUVWXYZ234567", k=16))
                key = g.node(["AccountAccessKey", "AWSAccountAccessKey"], id=kid, accesskeyid=kid, status=R.choice(["Active", "Active", "Inactive"]),
                             createdate=NOW - R.randint(10**6, 10**8), lastuseddate=NOW - R.randint(0, 10**7))
                g.rel(u, "AWS_ACCESS_KEY", key, "AWSUser", "AccountAccessKey")
        roles = []
        for nm in ["OrganizationAccountAccessRole", "ci-deployer", "readonly-auditor", "eks-node-role", "lambda-exec", "github-actions-oidc",
                   "okta-admin", "okta-developer", "okta-readonly", "data-pipeline", "backup-operator", "vendor-integration"] + [f"{w}-service-role" for w in R.sample(WORDS, 8)]:
            rl = A(("AWSRole", g.node(["AWSRole", "AWSPrincipal", "PermissionRole"], id=f"arn:aws:iam::{aid}:role/{nm}", arn=f"arn:aws:iam::{aid}:role/{nm}",
                                      name=nm, roleid="AROA" + "".join(R.choices("ABCDEFGHJKLMNPQRSTUVWXYZ234567", k=17)), path="/",
                                      createdate=NOW - R.randint(10**7, 10**8))))
            roles.append(rl)
            g.rel(rl, "POLICY", R.choice(policies), "AWSPrincipal", "AWSPolicy")
            if R.random() < 0.3:
                ip = g.node(["AWSInlinePolicy", "AWSPolicy"], id=f"{rl['arn']}/inline_policy/{nm}-inline", arn=f"{rl['arn']}/inline_policy/{nm}-inline",
                            name=f"{nm}-inline", type="inline")
                st = g.node(["AWSPolicyStatement"], id=f"{ip['id']}/statement/1", effect="Allow",
                            action=[R.choice(["s3:GetObject", "s3:PutObject", "kms:Decrypt", "sts:AssumeRole", "iam:PassRole"])], resource=["*"])
                g.rel(ip, "STATEMENT", st, "AWSPolicy", "AWSPolicyStatement")
                g.rel(rl, "POLICY", ip, "AWSPrincipal", "AWSPolicy")
            if nm == "vendor-integration":
                g.rel(rl, "TRUSTS_AWS_PRINCIPAL", froot, "AWSRole", "AWSPrincipal")
            if nm == "github-actions-oidc" or nm == "ci-deployer":
                gh_roles.append(rl)
            if nm.startswith("okta-"):
                okta_roles.append(rl)
        for u in users:
            if R.random() < 0.35:
                g.rel(u, "STS_ASSUMEROLE_ALLOW", R.choice(roles), "AWSPrincipal", "AWSRole")
        profiles = []
        for rl in roles[:6]:
            ipf = g.node(["AWSInstanceProfile"], id=f"arn:aws:iam::{aid}:instance-profile/{rl['name']}", arn=f"arn:aws:iam::{aid}:instance-profile/{rl['name']}",
                         instance_profile_name=rl["name"], path="/")
            g.rel(ipf, "ASSOCIATED_WITH", rl, "AWSInstanceProfile", "AWSRole")
            profiles.append((ipf, rl))
        # compute
        instances = []
        for _ in range(R.randint(30, 40)):
            sn, pub = R.choice(subnets_all)
            iid = f"i-{R.getrandbits(68):017x}"
            exposed = pub and R.random() < 0.5
            inst = A(("AWSEC2Instance", g.node(["AWSEC2Instance", "EC2Instance", "ComputeInstance"], id=iid, instanceid=iid,
                                               instancetype=R.choice(["t3.micro", "t3.medium", "m5.large", "m5.xlarge", "c6i.2xlarge", "r6g.large"]),
                                               state=R.choice(["running"] * 6 + ["stopped"]), region=sn["region"], availabilityzone=sn["availability_zone"],
                                               privateipaddress=f"10.{R.randint(0, 255)}.{R.randint(0, 255)}.{R.randint(1, 254)}",
                                               publicipaddress=f"203.0.113.{R.randint(1, 254)}" if pub else None,
                                               imageid=f"ami-{R.getrandbits(48):012x}", launchtime=NOW - R.randint(10**5, 10**8),
                                               exposed_internet=exposed or None, exposed_internet_type=["direct"] if exposed else None,
                                               imdsv2required=R.random() < 0.6, name=f"{R.choice(WORDS)}-{R.choice(WORDS)}-{R.randint(1, 9)}")))
            instances.append(inst)
            g.rel(inst, "PART_OF_SUBNET", sn, "AWSEC2Instance", "AWSEC2Subnet")
            for sg in R.sample([s for s in sgs_all if s["region"] == sn["region"]], R.randint(1, 2)):
                g.rel(inst, "MEMBER_OF_EC2_SECURITY_GROUP", sg, "AWSEC2Instance", "AWSEC2SecurityGroup")
            if R.random() < 0.6:
                ipf, rl = R.choice(profiles)
                g.rel(inst, "INSTANCE_PROFILE", ipf, "AWSEC2Instance", "AWSInstanceProfile")
                g.rel(inst, "STS_ASSUMEROLE_ALLOW", rl, "AWSEC2Instance", "AWSRole")
            tag = g.node(["AWSTag", "Tag"], id=f"{iid}|Name:{inst['name']}", key="Name", value=inst["name"], region=sn["region"])
            g.rel(inst, "TAGGED", tag, "AWSEC2Instance", "AWSTag")
        for k in range(R.randint(4, 8)):
            kp = A(("AWSEC2KeyPair", g.node(["AWSEC2KeyPair", "EC2KeyPair", "KeyPair"], id=f"arn:aws:ec2:us-east-1:{aid}:key-pair/key-{k}",
                                            keyname=f"{R.choice(FIRST)}-laptop-{k}", region="us-east-1",
                                            keyfingerprint=":".join(f"{R.getrandbits(8):02x}" for _ in range(16)))))
            for inst in R.sample(instances, R.randint(1, 5)):
                g.rel(kp, "SSH_LOGIN_TO", inst, "AWSEC2KeyPair", "AWSEC2Instance")
        for k in range(3):
            nm = f"{R.choice(WORDS)}-alb-{k}"
            lb = A(("AWSLoadBalancerV2", g.node(["AWSLoadBalancerV2", "LoadBalancerV2", "LoadBalancer"], id=f"{nm}-{R.getrandbits(32):08x}.us-east-1.elb.amazonaws.com",
                                                name=nm, dnsname=f"{nm}.us-east-1.elb.amazonaws.com", scheme=R.choice(["internet-facing", "internal"]),
                                                type="application", region="us-east-1", exposed_internet=True)))
            for inst in R.sample(instances, R.randint(2, 6)):
                g.rel(lb, "EXPOSE", inst, "AWSLoadBalancerV2", "AWSEC2Instance", port=443)
            g.rel(lb, "MEMBER_OF_EC2_SECURITY_GROUP", R.choice(sgs_all), "AWSLoadBalancerV2", "AWSEC2SecurityGroup")
        for k in range(R.randint(6, 10)):
            nm = f"{R.choice(WORDS)}-fn-{k}"
            fn = A(("AWSLambda", g.node(["AWSLambda", "Function"], id=f"arn:aws:lambda:us-east-1:{aid}:function:{nm}", arn=f"arn:aws:lambda:us-east-1:{aid}:function:{nm}",
                                        name=nm, runtime=R.choice(["python3.12", "nodejs20.x", "java21", "go1.x"]), memorysize=R.choice([128, 256, 512, 1024]),
                                        timeout=R.choice([3, 30, 300]), region="us-east-1", lastmodified=NOW - R.randint(10**5, 10**8))))
            g.rel(fn, "STS_ASSUMEROLE_ALLOW", R.choice(roles), "AWSLambda", "AWSRole")
        for k in range(R.randint(3, 5)):
            nm = f"{R.choice(WORDS)}-db-{k}"
            db = A(("AWSRDSInstance", g.node(["AWSRDSInstance", "RDSInstance", "Database"], id=f"arn:aws:rds:us-east-1:{aid}:db:{nm}", arn=f"arn:aws:rds:us-east-1:{aid}:db:{nm}",
                                             db_instance_identifier=nm, engine=R.choice(["postgres", "mysql", "aurora-postgresql"]), engine_version="16.3",
                                             db_instance_class=R.choice(["db.t3.medium", "db.r6g.large"]), publicly_accessible=R.random() < 0.15,
                                             storage_encrypted=R.random() < 0.8, multi_az=R.random() < 0.5, region="us-east-1",
                                             allocated_storage=R.choice([20, 100, 500, 2000]))))
            g.rel(db, "MEMBER_OF_EC2_SECURITY_GROUP", R.choice([s for s in sgs_all if s["name"] == "db"]), "AWSRDSInstance", "AWSEC2SecurityGroup")
            if db["storage_encrypted"]:
                g.rel(db, "ENCRYPTED_BY", R.choice(kms), "AWSRDSInstance", "AWSKMSKey")
        buckets = []
        for k in range(R.randint(18, 26)):
            nm = f"acme-{a['name'].split('-')[1]}-{R.choice(WORDS)}-{R.getrandbits(16):04x}"
            anon = R.random() < 0.12
            b = A(("AWSS3Bucket", g.node(["AWSS3Bucket", "S3Bucket", "ObjectStorage"], id=nm, name=nm, arn=f"arn:aws:s3:::{nm}", region=R.choice(REGIONS),
                                         creationdate=f"2024-{R.randint(1, 12):02d}-{R.randint(1, 28):02d}T10:00:00+00:00",
                                         anonymous_access=anon, anonymous_actions=["s3:GetObject"] if anon else None,
                                         versioning_status=R.choice(["Enabled", "Suspended", None]), default_encryption=R.random() < 0.85,
                                         block_public_acls=not anon, block_public_policy=not anon, object_count=R.randint(0, 2_000_000),
                                         size_bytes=R.randint(0, 5 * 10**12))))
            buckets.append(b)
            if b["default_encryption"] and R.random() < 0.5:
                g.rel(b, "ENCRYPTED_BY", R.choice(kms), "AWSS3Bucket", "AWSKMSKey")
        for p in R.sample(users + roles, 14):
            g.rel(p, R.choice(["CAN_READ", "CAN_READ", "CAN_WRITE"]), R.choice(buckets), "AWSPrincipal", "AWSS3Bucket")
    # GitHub
    org = g.node(["GitHubOrganization", "Tenant"], id="https://github.com/acme-corp-test", username="acme-corp-test", url="https://github.com/acme-corp-test")
    langs = {n: g.node(["ProgrammingLanguage"], id=n, name=n) for n in ["Python", "Go", "TypeScript", "Java", "HCL", "Shell", "Rust"]}
    gh_users = []
    seen = set()
    for _ in range(55):
        f, l, email = person()
        login = f"{f[:3]}{l[:5]}{R.randint(1, 99)}"
        if login in seen:
            continue
        seen.add(login)
        u = g.node(["GitHubUser", "UserAccount"], id=f"https://github.com/{login}", username=login, fullname=f"{f.title()} {l.title()}",
                   email=email if R.random() < 0.6 else None, has_2fa_enabled=R.random() < 0.85, is_site_admin=False,
                   role=R.choice(["MEMBER"] * 8 + ["ADMIN"]), company="Acme Corp" if R.random() < 0.7 else None)
        gh_users.append(u)
        g.rel(u, "ADMIN_OF" if u["role"] == "ADMIN" else "MEMBER_OF", org, "GitHubUser", "GitHubOrganization")
    outside = []
    for k in range(5):
        u = g.node(["GitHubUser", "UserAccount"], id=f"https://github.com/contractor-{k}", username=f"contractor-{k}", fullname=f"Contractor {k}",
                   has_2fa_enabled=R.random() < 0.5, is_site_admin=False)
        g.rel(u, "UNAFFILIATED", org, "GitHubUser", "GitHubOrganization")
        outside.append(u)
    teams = []
    for nm in ["platform", "security", "data", "frontend", "backend", "sre", "ml", "it"]:
        t = g.node(["GitHubTeam", "UserGroup"], id=f"https://github.com/orgs/acme-corp-test/teams/{nm}", name=nm, url=f"https://github.com/orgs/acme-corp-test/teams/{nm}",
                   description=f"The {nm} team")
        teams.append(t)
        g.rel(org, "RESOURCE", t, "GitHubOrganization", "GitHubTeam")
        for u in R.sample(gh_users, R.randint(4, 10)):
            g.rel(u, R.choice(["MEMBER"] * 5 + ["MAINTAINER"]), t, "GitHubUser", "GitHubTeam")
    repos = []
    for k in range(40):
        nm = f"{R.choice(WORDS)}-{R.choice(['service', 'infra', 'lib', 'ui', 'tools', 'terraform', 'pipeline'])}{'' if k < 20 else '-' + str(k)}"
        if any(r["name"] == nm for r in repos):
            nm += f"-{k}"
        priv = R.random() < 0.8
        rp = g.node(["GitHubRepository", "CodeRepository"], id=f"https://github.com/acme-corp-test/{nm}", name=nm, fullname=f"acme-corp-test/{nm}",
                    url=f"https://github.com/acme-corp-test/{nm}", private=priv, archived=R.random() < 0.1, defaultbranch="main",
                    primarylanguage=R.choice(list(langs)), createdat=f"20{R.randint(18, 25)}-{R.randint(1, 12):02d}-01T00:00:00Z",
                    stargazers_count=R.randint(0, 300), forks_count=R.randint(0, 40), visibility="private" if priv else "public")
        repos.append(rp)
        g.rel(rp, "OWNER", org, "GitHubRepository", "GitHubOrganization")
        g.rel(rp, "LANGUAGE", langs[rp["primarylanguage"]], "GitHubRepository", "ProgrammingLanguage")
        br = g.node(["GitHubBranch"], id=f"{rp['id']}/branch/main", name="main", protected=R.random() < 0.7)
        g.rel(rp, "BRANCH", br, "GitHubRepository", "GitHubBranch")
        for t in R.sample(teams, R.randint(1, 3)):
            g.rel(t, R.choice(["READ", "WRITE", "WRITE", "MAINTAIN", "ADMIN", "TRIAGE"]), rp, "GitHubTeam", "GitHubRepository")
        for u in R.sample(gh_users, R.randint(0, 3)):
            g.rel(u, R.choice(["DIRECT_COLLAB_WRITE", "DIRECT_COLLAB_ADMIN", "DIRECT_COLLAB_READ"]), rp, "GitHubUser", "GitHubRepository")
        if R.random() < 0.15:
            g.rel(R.choice(outside), R.choice(["OUTSIDE_COLLAB_WRITE", "OUTSIDE_COLLAB_READ"]), rp, "GitHubUser", "GitHubRepository")
        if gh_roles and R.random() < 0.2:
            g.rel(rp, "ASSUMED_ROLE_WITH_WEB_IDENTITY", R.choice(gh_roles), "GitHubRepository", "AWSRole")
    # Okta
    oorg = g.node(["OktaOrganization", "Tenant"], id="acme-corp-test.okta.com", name="acme-corp-test")
    ogroups = []
    for nm in ["Everyone", "Engineering", "Security", "Finance", "AWS-Admins", "AWS-Developers", "AWS-ReadOnly", "GitHub-Users", "Contractors", "Sales"]:
        og = g.node(["OktaGroup", "UserGroup"], id=f"00g{R.getrandbits(56):014x}", name=nm, description=f"{nm} group", group_type="OKTA_GROUP")
        ogroups.append(og)
        g.rel(oorg, "RESOURCE", og, "OktaOrganization", "OktaGroup")
    apps = []
    for nm in ["AWS IAM Identity Center", "GitHub Enterprise", "Slack", "Google Workspace", "Jira", "Datadog", "PagerDuty", "Salesforce"]:
        ap = g.node(["OktaApplication", "ThirdPartyApp"], id=f"0oa{R.getrandbits(56):014x}", name=nm.lower().replace(" ", "_"), label=nm,
                    status="ACTIVE", sign_on_mode=R.choice(["SAML_2_0", "OPENID_CONNECT"]))
        apps.append(ap)
        g.rel(oorg, "RESOURCE", ap, "OktaOrganization", "OktaApplication")
    for og in ogroups:
        for ap in R.sample(apps, R.randint(1, 3)):
            g.rel(og, "APPLICATION", ap, "OktaGroup", "OktaApplication")
    for og, rn in zip(ogroups[4:7], ["okta-admin", "okta-developer", "okta-readonly"]):
        for rl in okta_roles:
            if rl["name"] == rn:
                g.rel(og, "ALLOWED_BY", rl, "OktaGroup", "AWSRole")
    for i in range(60):
        f, l, email = person()
        ou = g.node(["OktaUser", "UserAccount"], id=f"00u{R.getrandbits(56):014x}", first_name=f.title(), last_name=l.title(), login=f"{f}.{l}{i}@example-corp.test",
                     email=f"{f}.{l}{i}@example-corp.test", status=R.choice(["ACTIVE"] * 9 + ["SUSPENDED", "DEPROVISIONED"]),
                     department=R.choice(["Engineering", "Security", "Finance", "Sales", "IT"]), created=NOW - R.randint(10**6, 10**8),
                     last_login=NOW - R.randint(0, 10**7))
        g.rel(oorg, "RESOURCE", ou, "OktaOrganization", "OktaUser")
        for og in [ogroups[0]] + R.sample(ogroups[1:], R.randint(1, 3)):
            g.rel(ou, "MEMBER_OF_OKTA_GROUP", og, "OktaUser", "OktaGroup")
        if R.random() < 0.4:
            g.rel(ou, "APPLICATION", R.choice(apps), "OktaUser", "OktaApplication")
    # GCP
    gorg = g.node(["GCPOrganization", "Tenant"], id="organizations/123456789012", displayname="acme-corp-test.example", lifecyclestate="ACTIVE")
    for pn in ["acme-data-prod", "acme-ml-sandbox", "acme-shared-vpc"]:
        pr = g.node(["GCPProject", "Tenant"], id=pn, projectid=pn, displayname=pn, projectnumber=str(R.randint(10**11, 10**12)), lifecyclestate="ACTIVE")
        g.rel(gorg, "RESOURCE", pr, "GCPOrganization", "GCPProject")
        for k in range(R.randint(6, 10)):
            nm = f"{R.choice(WORDS)}-vm-{k}"
            gi = g.node(["GCPInstance", "ComputeInstance", "Instance"], id=f"projects/{pn}/zones/us-central1-a/instances/{nm}", instancename=nm,
                        zone_name="us-central1-a", status=R.choice(["RUNNING", "RUNNING", "TERMINATED"]),
                        hostname=f"{nm}.c.{pn}.internal", exposed_internet=R.random() < 0.2)
            g.rel(pr, "RESOURCE", gi, "GCPProject", "GCPInstance")
        for k in range(R.randint(4, 8)):
            nm = f"{pn}-{R.choice(WORDS)}-{k}"
            gb = g.node(["GCPBucket", "ObjectStorage"], id=nm, bucket_id=nm, location=R.choice(["US", "EU", "US-CENTRAL1"]),
                        storage_class=R.choice(["STANDARD", "NEARLINE", "COLDLINE"]), iam_config_uniform_bucket_level_access=R.random() < 0.7,
                        versioning_enabled=R.random() < 0.4)
            g.rel(pr, "RESOURCE", gb, "GCPProject", "GCPBucket")
    return g


def _split_counts(total, weights):
    """Split `total` in proportion to `weights`. The canonical 14,998,500 split is exact."""
    denom = sum(weights)
    raw = [total * w // denom for w in weights]
    rem = total - sum(raw)
    order = sorted(range(len(weights)), key=lambda i: (-weights[i], i))
    i = 0
    while rem > 0:
        raw[order[i % len(order)]] += 1
        rem -= 1
        i += 1
    return raw


def plan_live_scale(n_nodes=LIVE_NODES, n_edges=LIVE_EDGES):
    """A Cartography-shaped graph whose node and relationship totals are exact.

    Every resource has one RESOURCE edge to an AWSAccount. Further edges run from
    resource to resource on distinct strides, so the loader can stream them.
    Account 0 is a supernode. One response from the explorer still returns a page,
    not this graph.
    """
    n_nodes, n_edges = int(n_nodes), int(n_edges)
    if n_nodes < 4:
        raise ValueError("live-scale needs at least 4 nodes")
    if n_nodes == LIVE_NODES:
        accounts = LIVE_ACCOUNTS
    else:
        accounts = max(2, min(n_nodes // 5, max(2, n_nodes // 10_000)))
    if accounts >= n_nodes - 1:
        accounts = max(2, n_nodes // 2)
    resources = n_nodes - accounts
    if resources < 2:
        raise ValueError("live-scale needs at least 2 resources besides the accounts")
    if n_edges < resources:
        raise ValueError(f"need at least {resources:,} edges (one RESOURCE per resource); got {n_edges:,}")
    max_extra = resources * (resources - 1)
    if n_edges - resources > max_extra:
        raise ValueError(
            f"cannot place {n_edges:,} edges on {resources:,} resources without repeating an endpoint "
            f"(maximum is {resources + max_extra:,})")
    counts = _split_counts(resources, [w for _, _, w in LABEL_WEIGHTS])
    labels, start = [], 0
    for (label, labs, _), count in zip(LABEL_WEIGHTS, counts):
        if count <= 0:
            continue
        labels.append({"label": label, "labels": labs, "start": start, "count": count})
        start += count
    if n_nodes == LIVE_NODES and resources == LIVE_NODES - LIVE_ACCOUNTS:
        super_n = LIVE_SUPER
    else:
        super_n = max(1, min(resources - (accounts - 1), max(1, resources // 7)))
    extra = n_edges - resources
    base, rem = divmod(extra, resources)
    plan = {
        "nodes": n_nodes, "edges": n_edges, "accounts": accounts, "resources": resources,
        "super_resources": super_n, "labels": labels, "extra_base": base, "extra_rem": rem,
    }
    if node_total(plan) != n_nodes or edge_total(plan) != n_edges:
        raise ValueError(f"plan missed the requested totals: {node_total(plan)} nodes / {edge_total(plan)} edges")
    return plan


def node_total(plan):
    return plan["accounts"] + sum(s["count"] for s in plan["labels"])


def edge_total(plan):
    return plan["resources"] + plan["extra_base"] * plan["resources"] + plan["extra_rem"]


def owner_index(i, plan):
    if i < plan["super_resources"]:
        return 0
    others = plan["accounts"] - 1
    return 1 + (i - plan["super_resources"]) % others


def label_at(plan, i):
    lo, hi = 0, len(plan["labels"]) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        spec = plan["labels"][mid]
        if i < spec["start"]:
            hi = mid - 1
        elif i >= spec["start"] + spec["count"]:
            lo = mid + 1
        else:
            return spec
    raise IndexError(i)


def extra_strides(k, mod):
    """k distinct non-zero steps modulo `mod`, so a node's extra edges do not share a target."""
    k = int(k)
    if k <= 0:
        return []
    key = (k, mod)
    hit = _STRIDE_CACHE.get(key)
    if hit is not None:
        return hit
    got, s, guard = [], 1, 0
    while len(got) < k and guard < mod + k + 5:
        stride = s % mod
        if stride and stride not in got:
            got.append(stride)
        s += 997
        guard += 1
    if len(got) < k:
        raise ValueError(f"need {k} distinct strides modulo {mod}, found {len(got)}")
    _STRIDE_CACHE[key] = tuple(got)
    return _STRIDE_CACHE[key]


def iter_edges(plan):
    """Every relationship as (src_label, src_id, type, dst_label, dst_id), in load order."""
    for spec in plan["labels"]:
        for i in range(spec["start"], spec["start"] + spec["count"]):
            yield ("AWSAccount", f"aws-account:{owner_index(i, plan)}", "RESOURCE", spec["label"], f"{spec['label']}:{i}")
    if plan["extra_base"] == 0 and plan["extra_rem"] == 0:
        return
    resources = plan["resources"]
    base = extra_strides(plan["extra_base"], resources)
    hi = extra_strides(plan["extra_base"] + (1 if plan["extra_rem"] else 0), resources)
    for i in range(resources):
        src = label_at(plan, i)
        strides = hi if i < plan["extra_rem"] else base
        for n, stride in enumerate(strides):
            j = (i + stride) % resources
            dst = label_at(plan, j)
            yield (src["label"], f"{src['label']}:{i}", EXTRA_TYPES[n % len(EXTRA_TYPES)], dst["label"], f"{dst['label']}:{j}")


def plan_summary(plan):
    return {
        "nodes": plan["nodes"], "edges": plan["edges"], "accounts": plan["accounts"],
        "resources": plan["resources"], "super_resources": plan["super_resources"],
        "extra_per_resource": plan["extra_base"], "resources_with_one_more_edge": plan["extra_rem"],
        "labels": [{"label": s["label"], "count": s["count"]} for s in plan["labels"]],
    }


def load_live_scale(session, plan, batch=5000):
    """Stream the plan into Neo4j. Nodes are created first, then indexes, then edges."""
    def run(q, **params):
        session.run(q, **params).consume()

    t0 = time.time()
    accounts = [{
        "id": f"aws-account:{a}",
        "name": "supernode" if a == 0 else f"account-{a}",
        "accountid": f"{a:012d}",
    } for a in range(plan["accounts"])]
    for c in chunks(accounts, batch):
        run("UNWIND $rows AS row CREATE (n:AWSAccount) SET n = row", rows=c)
    print(f"  accounts: {plan['accounts']:,}", flush=True)

    created = plan["accounts"]
    for spec in plan["labels"]:
        lab = ":".join(f"`{x}`" for x in spec["labels"])
        buf = []
        for i in range(spec["start"], spec["start"] + spec["count"]):
            buf.append({
                "id": f"{spec['label']}:{i}",
                "name": f"{spec['label'].lower()}-{i}",
                "account": f"aws-account:{owner_index(i, plan)}",
                "region": REGIONS[i % len(REGIONS)],
            })
            if len(buf) >= batch:
                run(f"UNWIND $rows AS row CREATE (n:{lab}) SET n = row", rows=buf)
                created += len(buf)
                buf = []
                if created % 100_000 < batch:
                    print(f"  nodes {created:,} / {plan['nodes']:,}  ({time.time() - t0:.0f}s)", flush=True)
        if buf:
            run(f"UNWIND $rows AS row CREATE (n:{lab}) SET n = row", rows=buf)
            created += len(buf)
    print(f"  nodes done: {created:,} in {time.time() - t0:.0f}s", flush=True)

    for lab in ["AWSAccount", *[s["label"] for s in plan["labels"]]]:
        run(f"CREATE INDEX live_{lab}_id IF NOT EXISTS FOR (n:`{lab}`) ON (n.id)")
    print("  building id indexes (this waits until they are online) ...", flush=True)
    run("CALL db.awaitIndexes(7200)")

    n_edges = 0
    buckets = defaultdict(list)

    def flush(key):
        nonlocal n_edges
        rows = buckets.pop(key, None)
        if not rows:
            return
        sl, typ, dl = key
        run(
            f"UNWIND $rows AS row MATCH (a:`{sl}` {{id: row.s}}) MATCH (b:`{dl}` {{id: row.d}}) "
            f"CREATE (a)-[:`{typ}`]->(b)",
            rows=rows)
        n_edges += len(rows)
        if n_edges // 100_000 != (n_edges - len(rows)) // 100_000:
            print(f"  edges {n_edges:,} / {plan['edges']:,}  ({time.time() - t0:.0f}s)", flush=True)

    for sl, sid, typ, dl, did in iter_edges(plan):
        key = (sl, typ, dl)
        buckets[key].append({"s": sid, "d": did})
        if len(buckets[key]) >= batch:
            flush(key)
    for key in list(buckets):
        flush(key)
    print(f"  loaded {created:,} nodes / {n_edges:,} edges in {time.time() - t0:.0f}s", flush=True)
    return created, n_edges


def is_local(uri: str) -> bool:
    host = urlparse(uri).hostname or ""
    return host in ("localhost", "127.0.0.1", "::1")


def scaled(g: G, copies: int, hub: int) -> G:
    """Replicate the synthetic estate `copies` times (ids get a ~N suffix) and optionally hang `hub`
    extra EC2 instances off the first AWS account to create a supernode."""
    out = G()
    for i in range(copies):
        sfx = "" if i == 0 else f"~{i}"
        for labels, rows in g.nodes.items():
            out.nodes[labels].extend(rows if i == 0 else [{**r, "id": r["id"] + sfx} for r in rows])
        for key, rows in g.rels.items():
            out.rels[key].extend(rows if i == 0 else [(a + sfx, b + sfx, p) for a, b, p in rows])
    if hub:
        acct = g.nodes[next(k for k in g.nodes if k[0] == "AWSAccount")][0]
        for k in range(hub):
            iid = f"i-hub{k:07d}"
            out.node(["AWSEC2Instance", "EC2Instance", "ComputeInstance"], id=iid, instanceid=iid, name=f"fleet-{k}",
                     state=R.choice(["running"] * 6 + ["stopped"]), region=R.choice(REGIONS), instancetype=R.choice(["t3.micro", "m5.large", "c6g.xlarge"]))
            out.rel(acct, "RESOURCE", {"id": iid}, "AWSAccount", "AWSEC2Instance")
    return out


def load_fixtures(path: str):
    """Real-schema mock data exported from Cartography's own demo seeds + test fixtures (see tools/cartography_fixtures)."""
    import gzip
    import json
    with gzip.open(path, "rt") as f:
        return json.load(f)


def chunks(rows, n=10000):
    for i in range(0, len(rows), n):
        yield rows[i:i + n]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--wipe", action="store_true", help="delete all nodes first (local DB only)")
    ap.add_argument("--fixtures", nargs="?", const=os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "cartography-fixtures.json.gz"),
                    help="load the real-schema Cartography fixture snapshot instead of the synthetic estate")
    ap.add_argument("--scale", type=int, default=1, help="replicate the synthetic estate N times (~1.1k nodes / 2.4k rels each)")
    ap.add_argument("--hub", type=int, default=0, help="add N EC2 instances to the first AWS account (supernode test)")
    ap.add_argument("--live-scale", action="store_true",
                    help=f"stream the minimum supported graph: {LIVE_NODES:,} nodes and {LIVE_EDGES:,} relationships")
    ap.add_argument("--nodes", type=int, default=None, help="with --live-scale, exact node count (default 15000000)")
    ap.add_argument("--edges", type=int, default=None, help="with --live-scale, exact relationship count (default 60000000)")
    ap.add_argument("--dry-run", action="store_true", help="with --live-scale, print the plan and do not connect")
    args = ap.parse_args()
    if args.live_scale and args.fixtures:
        sys.exit("--live-scale loads its own graph; do not combine it with --fixtures")
    if args.live_scale and (args.scale != 1 or args.hub):
        sys.exit("--live-scale loads its own graph; do not combine it with --scale or --hub")
    if (args.nodes is not None or args.edges is not None or args.dry_run) and not args.live_scale:
        sys.exit("--nodes, --edges, and --dry-run are only used with --live-scale")
    if args.dry_run:
        import json
        plan = plan_live_scale(args.nodes or LIVE_NODES, args.edges or LIVE_EDGES)
        print(json.dumps(plan_summary(plan), indent=2))
        return
    from neo4j_env import Neo4jConfigError, neo4j_settings
    try:
        settings = neo4j_settings()
    except Neo4jConfigError as e:
        sys.exit(str(e))
    uri = settings.uri
    if not is_local(uri):
        sys.exit("Refusing to seed a non-local database: " + uri)
    t0 = time.time()
    with GraphDatabase.driver(uri, auth=(settings.user, settings.password)) as drv, drv.session(database=settings.database) as s:
        if args.wipe:
            s.run("MATCH (n) CALL (n) { DETACH DELETE n } IN TRANSACTIONS OF 10000 ROWS").consume()
        if args.fixtures:
            fx = load_fixtures(args.fixtures)
            s.run("CREATE INDEX seed_xid IF NOT EXISTS FOR (n:_Seed) ON (n._xid)").consume()
            s.run("CALL db.awaitIndexes(300)").consume()
            groups = defaultdict(list)
            for i, n in enumerate(fx["nodes"]):
                groups[tuple(n["l"])].append({**n["p"], "_xid": i})
            for labels, rows in groups.items():
                lab = "".join(f":`{x}`" for x in labels)
                for c in chunks(rows):
                    s.run(f"UNWIND $rows AS row CREATE (n:_Seed{lab}) SET n = row", rows=c).consume()
            by_type = defaultdict(list)
            for r in fx["rels"]:
                by_type[r["t"]].append({"s": r["s"], "d": r["d"], "p": r["p"]})
            for typ, rows in by_type.items():
                for c in chunks(rows):
                    s.run(f"UNWIND $rows AS row MATCH (a:_Seed {{_xid: row.s}}) MATCH (b:_Seed {{_xid: row.d}}) CREATE (a)-[r:`{typ}`]->(b) SET r = row.p", rows=c).consume()
            s.run("MATCH (n:_Seed) CALL (n) { REMOVE n:_Seed REMOVE n._xid } IN TRANSACTIONS OF 10000 ROWS").consume()
            s.run("DROP INDEX seed_xid IF EXISTS").consume()
            n_nodes, n_rels = len(fx["nodes"]), len(fx["rels"])
        elif args.live_scale:
            plan = plan_live_scale(args.nodes or LIVE_NODES, args.edges or LIVE_EDGES)
            print(f"live-scale plan: {plan['nodes']:,} nodes / {plan['edges']:,} relationships "
                  f"({plan['accounts']:,} accounts, account 0 owns {plan['super_resources']:,} resources)", flush=True)
            n_nodes, n_rels = load_live_scale(s, plan)
        else:
            g = build()
            if args.scale > 1 or args.hub:
                g = scaled(g, args.scale, args.hub)
            primaries = sorted({labels[0] for labels in g.nodes})
            for lab in primaries:  # Cartography creates an index on id for each label
                s.run(f"CREATE INDEX IF NOT EXISTS FOR (n:`{lab}`) ON (n.id)").consume()
            for lab in ("AWSPrincipal", "AWSPolicy", "AccountAccessKey"):
                s.run(f"CREATE INDEX IF NOT EXISTS FOR (n:`{lab}`) ON (n.id)").consume()
            s.run("CALL db.awaitIndexes(300)").consume()
            n_nodes = n_rels = 0
            for labels, rows in g.nodes.items():
                lab = ":".join(f"`{x}`" for x in labels)
                rows = [{k: v for k, v in r.items() if v is not None} for r in rows]
                for c in chunks(rows):
                    s.run(f"UNWIND $rows AS row MERGE (n:`{labels[0]}` {{id: row.id}}) SET n += row SET n:{lab}", rows=c).consume()
                n_nodes += len(rows)
            for (sl, typ, dl), rows in g.rels.items():
                data = [{"s": a, "d": b, "p": p} for a, b, p in rows]
                for c in chunks(data):
                    s.run(f"UNWIND $rows AS row MATCH (a:`{sl}` {{id: row.s}}) MATCH (b:`{dl}` {{id: row.d}}) "
                          f"MERGE (a)-[r:`{typ}`]->(b) SET r += row.p", rows=c).consume()
                n_rels += len(rows)
        total = s.run("MATCH (n) RETURN count(n) AS n").single()["n"]
        total_r = s.run("MATCH ()-[r]->() RETURN count(r) AS n").single()["n"]
    print(f"loaded {n_nodes} nodes / {n_rels} relationships in {time.time() - t0:.1f}s; "
          f"database now holds {total} nodes / {total_r} relationships")


if __name__ == "__main__":
    main()
