"""Dedicated T02.2 process fault probe. Never launches the application or providers."""
import argparse
import json
import os
from pathlib import Path
import sys
from uuid import UUID
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from jobs.banking_consumer import Consumer, Lease

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=['before-commit','after-commit'])
    parser.add_argument('event',type=UUID)
    parser.add_argument('customer',type=UUID)
    parser.add_argument('owner',type=UUID)
    parser.add_argument('generation',type=int)
    args=parser.parse_args()
    config=json.loads((ROOT/'.genesis/local/p01/config.json').read_text(encoding='utf-8'))
    consumer=Consumer(config['dsns']['dispatcher'],config['dsns']['worker'])
    lease=Lease(args.event,args.customer,args.generation,args.owner)
    try:
        if args.mode=='before-commit':
            with consumer._connection(lease) as connection:
                connection.execute('SELECT banking.complete_consumer(%s,%s,%s,NULL)',
                    (lease.event_id,lease.owner,lease.generation))
                # Genuine process exit: connection context/finally cannot run COMMIT.
                os._exit(17)
        consumer.complete(lease)
        # Commit returned, but parent receives no success acknowledgement.
        os._exit(18)
    except Exception as error:
        print(type(error).__name__)
        raise SystemExit(19)
