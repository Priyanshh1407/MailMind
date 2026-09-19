"""Audit the exact Python lock through OSV; submit package names/versions only."""
import argparse
import json
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
OSV = 'https://api.osv.dev/v1/querybatch'


def locked(path):
    packages=[]
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        if line and not line.startswith('#'):
            name,version=line.split('==',1)
            packages.append((name,version))
    return packages


def audit(lock=ROOT/'requirements.lock', policy=ROOT/'config/security-advisory-policy.json'):
    packages=locked(lock)
    body=json.dumps({'queries':[{'package':{'name':name,'ecosystem':'PyPI'},'version':version} for name,version in packages]}).encode()
    request=Request(OSV,data=body,headers={'Content-Type':'application/json'},method='POST')
    with urlopen(request,timeout=90) as response:results=json.load(response)['results']
    if len(results)!=len(packages):raise RuntimeError('OSV returned an incomplete result set')
    accepted=set(json.loads(Path(policy).read_text(encoding='utf-8'))['accepted_ids']) if Path(policy).is_file() else set()
    findings=[{'package':name,'version':version,'id':item['id'],'modified':item.get('modified')}
        for (name,version),result in zip(packages,results) for item in result.get('vulns',[])]
    unexpected=[item for item in findings if item['id'] not in accepted]
    stale=sorted(accepted-{item['id'] for item in findings})
    return {'source':OSV,'packages':len(packages),'findings':findings,'accepted_embedded_chroma_findings':len(findings)-len(unexpected),
            'unexpected_findings':unexpected,'policy_ids_not_returned':stale,'all_unexpected_clear':not unexpected}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lock',default=str(ROOT/'requirements.lock'))
    parser.add_argument('--policy',default=str(ROOT/'config/security-advisory-policy.json'))
    parser.add_argument('--report',default=str(ROOT/'docs/evaluation/security/dependency_audit.json'))
    args=parser.parse_args();result=audit(args.lock,args.policy)
    target=Path(args.report);target.parent.mkdir(parents=True,exist_ok=True);target.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result,indent=2))
    raise SystemExit(0 if result['all_unexpected_clear'] else 1)
