"""Verify a curated clean source snapshot in a new venv; never copy private files."""
import argparse,json,os,shutil,subprocess,sys,tempfile,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
DIRECTORIES=['src','api','scripts','tests','fixtures','config','.github','notebooks']
FILES=['requirements.txt','requirements.lock','requirements-dev.txt','Dockerfile','docker-compose.yml','.dockerignore','.gitignore','.gitattributes','.env.example','.python-version','.nvmrc','README.md','CONTRIBUTING.md','SECURITY.md','docs/PRIVACY_AND_LOCAL_ONLY.md','docs/SETUP_AND_RELEASE.md','docs/OPERATING_POLICIES.md','docs/LOCAL_SCALE_DECISION.md','start_all.bat','start_all.sh','end_all.bat','end_all.sh']


def snapshot(destination):
    destination=Path(destination);destination.mkdir(parents=True,exist_ok=True)
    ignore=shutil.ignore_patterns(
        '__pycache__','*.pyc','node_modules','dist','test-results',
        'playwright-report','reports','report','coverage','htmlcov',
        '.coverage*','*.log','*.db','*.sqlite*')
    for name in DIRECTORIES:
        if (ROOT/name).exists():shutil.copytree(ROOT/name,destination/name,ignore=ignore)
    shutil.copytree(ROOT/'frontend',destination/'frontend',ignore=ignore)
    for name in FILES:
        if (ROOT/name).exists():
            (destination/name).parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(ROOT/name,destination/name)
    for excluded in ['data','models','venv','.env','credentials.json','token.json','.git','.run']:
        if (destination/excluded).exists():raise AssertionError('Private root copied into clean snapshot')


def check(report,browser=True):
    started=time.monotonic();checks=[]
    with tempfile.TemporaryDirectory(prefix='mailmind-clean-release-') as directory:
        root=Path(directory);snapshot(root)
        subprocess.run([sys.executable,'-m','venv',str(root/'venv')],check=True)
        python=root/'venv'/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
        env=os.environ.copy()
        for key in ['GEMINI_API_KEY','GROQ_API_KEY','TELEGRAM_BOT_TOKEN','TELEGRAM_CHAT_ID','MAILMIND_MODEL_PATH','MAILMIND_ASSET_MANIFEST','MAILMIND_RETRIEVAL_POLICY_PATH']:
            env.pop(key,None)
        env.update({'MAILMIND_LOCAL_ONLY':'false','MAILMIND_DATA_DIR':str(root/'data'),'HF_HUB_OFFLINE':'1','HF_HUB_DISABLE_TELEMETRY':'1','PIP_DISABLE_PIP_VERSION_CHECK':'1'})
        def run(name,command,cwd=root,timeout=600):
            print('Checking '+name,flush=True);log=root/(name+'.log')
            with log.open('w',encoding='utf-8') as stream:
                result=subprocess.run(command,cwd=cwd,env=env,stdout=stream,stderr=subprocess.STDOUT,timeout=timeout)
            if result.returncode:
                print(log.read_text(encoding='utf-8',errors='replace')[-12000:],flush=True)
                raise RuntimeError('Clean release check failed: '+name)
            checks.append({'check':name,'passed':True})
        run('torch-cpu',[str(python),'-m','pip','--isolated','install','torch==2.14.0','--index-url','https://download.pytorch.org/whl/cpu'])
        run('install',[str(python),'-m','pip','--isolated','install','-r','requirements-dev.txt','--index-url','https://pypi.org/simple'])
        run('pip-check',[str(python),'-m','pip','check'])
        run('backend-strict',[str(python),'-m','tests.run_baseline','--strict'])
        run('interview-demo',[str(python),'-m','scripts.demo_interview'])
        npm=shutil.which('npm.cmd' if os.name=='nt' else 'npm')
        for task in ['ci','test','build','lint']:
            run('frontend-'+task,[npm,task] if task=='ci' else [npm,'run',task],root/'frontend')
        if browser:run('frontend-browser',[npm,'run','test:browser'],root/'frontend')
        counts={'backend':(root/'backend-strict.log').read_text(encoding='utf-8',errors='replace').split('Ran ')[-1].split(' tests')[0],
                'frontend_browser':(root/'frontend-browser.log').read_text(encoding='utf-8',errors='replace').split(' passed (')[0].splitlines()[-1].strip() if browser else 'not_run'}
    result={'scope':'fresh curated snapshot and newly installed venv; current uncommitted work included; private files excluded','all_passed':True,'checks':checks,'counts':counts,'seconds':time.monotonic()-started}
    report=Path(report);report.parent.mkdir(parents=True,exist_ok=True);report.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8');return result

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--report',default='docs/evaluation/phase9/clean_release.json');parser.add_argument('--skip-browser',action='store_true');args=parser.parse_args();print(json.dumps(check(args.report,not args.skip_browser),indent=2))
