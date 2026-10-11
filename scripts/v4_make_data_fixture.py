"""Copy a bounded real NetFlow prefix plus cross-file counter controls, no labels."""
import argparse
import csv
from datetime import datetime,timedelta,timezone
import hashlib
import json
from pathlib import Path


def build(source,destination,count):
    source=Path(source).resolve(strict=True);destination=Path(destination).resolve()
    if count<2:raise ValueError('at least two NetFlow records required')
    if destination==source.parent or source.parent in destination.parents:
        raise ValueError('fixture must be outside original data directory')
    destination.mkdir(parents=True,exist_ok=False)
    before=source.stat();digest=hashlib.sha256();remaining=count
    with source.open(encoding='utf-8-sig',newline='') as inp:
        reader=csv.reader(inp,strict=True);header=next(reader)
        for number,n in enumerate((count//2,count-count//2),1):
            path=destination/f'netflow_5tuple_minute_part{number}.csv'
            with path.open('w',encoding='utf-8',newline='') as out:
                writer=csv.writer(out);writer.writerow(header)
                for _ in range(n):
                    row=next(reader,None)
                    if row is None:raise ValueError('source has fewer than requested NetFlow records')
                    writer.writerow(row);digest.update(json.dumps(row,ensure_ascii=False,separators=(',',':')).encode());remaining-=1
    # A single native counter sequence spans both files, including an exact
    # timestamp tie at the split. Rules/timezone are ordinary pipeline inputs.
    start=datetime(2026,9,17,4,tzinfo=timezone.utc)
    for number,indices in ((1,range(16)),(2,range(15,31))):
        with (destination/f'node_metrics_control{number}.csv').open('w',newline='') as out:
            writer=csv.DictWriter(out,fieldnames=['timestamp','node','cpu_usage','disk_reads_completed_total'])
            writer.writeheader()
            for i in indices:writer.writerow(dict(timestamp=(start+timedelta(minutes=i)).isoformat(),node='br1',cpu_usage=90 if i in (18,19) else 10,disk_reads_completed_total=i*100))
    after=source.stat()
    if (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns)!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns):raise ValueError('source changed while copying fixture')
    result=dict(source=str(source),source_size=before.st_size,source_mtime_ns=before.st_mtime_ns,
        netflow_records=count,prefix_parsed_cells_sha256=digest.hexdigest(),nonflow_records=32,
        purpose='data equivalence and throughput only; not fault scoring or full batch')
    (destination/'fixture.json').write_text(json.dumps(result,indent=2)+'\n')
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--netflow-file',type=Path,required=True)
    parser.add_argument('--output-dir',type=Path,required=True)
    parser.add_argument('--netflow-rows',type=int,default=1000000)
    args=parser.parse_args();print(json.dumps(build(args.netflow_file,args.output_dir,args.netflow_rows)))
