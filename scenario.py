import base64
import json
from sampledata import samples

def response(raw, status=200, headers=None, **options):
    return dict(base64=base64.b64encode(raw).decode(),status=status,
                headers=headers or {},chunk_bytes=3,**options)

def scenario():
    fixtures = samples()
    routes = {}
    routes['GET /forecast'] = [response(fixtures['sample.grib2'],status=200)]
    return routes

def main():
    import argparse
    from transport import ReplaySource
    parser = argparse.ArgumentParser(description='来源协议本地演练入口')
    parser.add_argument('--export', action='store_true')
    args = parser.parse_args()
    routes = scenario()
    if args.export:
        print(json.dumps(routes,ensure_ascii=False,sort_keys=True))
        return
    with ReplaySource(routes) as server:
        print(json.dumps({'url':server.url,'routes':list(routes)},ensure_ascii=False),flush=True)
        try:
            server.thread.join()
        except KeyboardInterrupt:
            pass

if __name__ == '__main__':
    main()
