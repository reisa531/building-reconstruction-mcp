"""Local equivalent of MCP jobs, useful for repeatable offline runs."""
import argparse,json,time
from pathlib import Path
from .jobs import JobManager

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--operation',default='run_pipeline')
    parser.add_argument('--params',type=Path,required=True)
    args=parser.parse_args()
    params=json.loads(args.params.read_text(encoding='utf-8-sig'))
    manager=JobManager(args.root)
    try:
        jid=manager.start(args.operation,params)
        while True:
            job=manager.load(jid)
            if job['status'] in {'completed','failed','interrupted'}: break
            time.sleep(.2)
        print(json.dumps(job,ensure_ascii=False,indent=2))
        if job['status']!='completed': raise SystemExit(1)
    finally: manager.pool.shutdown(wait=True)

if __name__=='__main__': main()
