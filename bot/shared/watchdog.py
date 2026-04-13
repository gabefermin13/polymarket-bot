#!/usr/bin/env python3
import os, subprocess, time, fcntl

LOG = '/tmp/watchdog.log'
CBOTS_PAUSED_FLAG = '/root/cbots_paused'  # touch this file to pause C-bots

def log(msg):
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    line = '[' + ts + '] ' + msg
    print(line, flush=True)
    with open(LOG, 'a') as f:
        f.write(line + chr(10))

def get_bot_pids(bot_dir):
    pids = []
    for pid in os.listdir('/proc'):
        if not pid.isdigit():
            continue
        try:
            if os.readlink('/proc/' + pid + '/cwd') == bot_dir:
                raw = open('/proc/' + pid + '/cmdline', 'rb').read()
                args = raw.split(b'\x00')
                # Must be python3 with main.py as the first argument (not bash wrapping a command)
                if args and b'python' in args[0] and len(args) > 1 and args[1].strip(b'/') == b'main.py':
                    pids.append(int(pid))
        except Exception:
            pass
    return sorted(pids)

def get_dbot_pids(bot_dir):
    pids = []
    for pid in os.listdir('/proc'):
        if not pid.isdigit():
            continue
        try:
            if os.readlink('/proc/' + pid + '/cwd') == bot_dir:
                raw = open('/proc/' + pid + '/cmdline', 'rb').read()
                args = raw.split(b'\x00')
                if args and b'python' in args[0] and len(args) > 1 and b'd_main' in args[1]:
                    pids.append(int(pid))
        except Exception:
            pass
    return sorted(pids)

def start_bot(label, d, log_path):
    cmd = 'cd ' + d + ' && export $(cat .env | xargs 2>/dev/null) && nohup python3 main.py >> ' + log_path + ' 2>&1 &'
    subprocess.Popen(['bash', '-c', cmd])
    log('  Started ' + label)

def start_dbot(label, d, log_path):
    cmd = 'cd ' + d + ' && set -a && source .env && set +a && nohup python3 d_main.py >> ' + log_path + ' 2>&1 &'
    subprocess.Popen(['bash', '-c', cmd])
    log('  Started ' + label)

def is_running(pattern):
    r = subprocess.run(['pgrep', '-f', pattern], capture_output=True)
    return r.returncode == 0

def start_script(label, cmd):
    subprocess.Popen(['bash', '-c', cmd + ' &'])
    log('  Started ' + label)

BOTS = [
    ('C1', '/root/kalshiedge_whalewallet_c1',  '/root/kalshiedge_whalewallet_c1/logs_c1/bot.log'),
    ('C2', '/root/kalshiedge_whalewallet',      '/root/kalshiedge_whalewallet/logs/bot.log'),
    ('C3', '/root/kalshiedge_whalewallet_c3',   '/root/kalshiedge_whalewallet_c3/logs_c3/bot.log'),
    ('C4', '/root/kalshiedge_whalewallet_c4',   '/root/kalshiedge_whalewallet_c4/logs_c4/bot.log'),
]

DBOTS = [
    # PAUSED # ('D1', '/root/kalshiedge_dbot_d1', '/root/kalshiedge_dbot_d1/logs_d1/bot.log'),
    ('D2', '/root/kalshiedge_dbot_d2', '/root/kalshiedge_dbot_d2/logs_d2/bot.log'),
    # PAUSED # ('D3', '/root/kalshiedge_dbot_d3', '/root/kalshiedge_dbot_d3/logs_d3/bot.log'),
    # PAUSED # ('D4', '/root/kalshiedge_dbot_d4', '/root/kalshiedge_dbot_d4/logs_d4/bot.log'),
    # PAUSED # ('D5', '/root/kalshiedge_dbot_d5', '/root/kalshiedge_dbot_d5/logs_d5/bot.log'),
    # PAUSED # ('D6', '/root/kalshiedge_dbot_d6', '/root/kalshiedge_dbot_d6/logs_d6/bot.log'),
    # PAUSED # ('D7', '/root/kalshiedge_dbot_d7', '/root/kalshiedge_dbot_d7/logs_d7/bot.log'),
]

SCRIPTS = [
    ('scan16_auto',      'scan16_auto.py',
     'cd /root/kalshiedge_whalewallet && PYTHONUNBUFFERED=1 python3 -u scan16_auto.py > /tmp/scan16_out.txt 2>&1'),
    ('rotation_manager', 'rotation_manager.py',
     'nohup python3 /root/kalshiedge_whalewallet/rotation_manager.py > /tmp/rotation_out.txt 2>&1'),
    ('dashboard',        'uvicorn dashboard:app',
     'nohup uvicorn dashboard:app --host 0.0.0.0 --port 8080 --app-dir /root > /tmp/dashboard.log 2>&1'),
    ('whalebot',         'w_main.py',
     'cd /root/kalshiedge_whalebot && set -a && source .env && set +a && nohup python3 w_main.py >> logs_w/bot.log 2>&1'),
]

def main():
    # Prevent concurrent watchdog runs (cron fires every 5 min)
    lock_file = open('/tmp/watchdog.lock', 'w')
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        log('Watchdog already running — exiting')
        lock_file.close()
        return

    restarted = []
    if os.path.exists(CBOTS_PAUSED_FLAG):
        log('C-bots paused (flag: ' + CBOTS_PAUSED_FLAG + ')')
    else:
        for label, d, log_path in BOTS:
            pids = get_bot_pids(d)
            if len(pids) > 1:
                for p in pids[1:]:
                    try: os.kill(p, 15)
                    except Exception: pass
                log('DUPE: ' + label + ' had ' + str(len(pids)) + ' procs - killed ' + str(pids[1:]) + ', kept ' + str(pids[0]))
            elif len(pids) == 0:
                log('DEAD: ' + label + ' - restarting...')
                start_bot(label, d, log_path)
                restarted.append(label)
                time.sleep(2)

    for label, d, log_path in DBOTS:
        flag = '/root/' + label.lower() + '_paused'
        if os.path.exists(flag):
            log(label + ' paused (flag: ' + flag + ')')
            continue
        pids = get_dbot_pids(d)
        if len(pids) > 1:
            for p in pids[1:]:
                try: os.kill(p, 15)
                except Exception: pass
            log('DUPE: ' + label + ' had ' + str(len(pids)) + ' procs - killed ' + str(pids[1:]) + ', kept ' + str(pids[0]))
        elif len(pids) == 0:
            log('DEAD: ' + label + ' - restarting...')
            start_dbot(label, d, log_path)
            restarted.append(label)
            time.sleep(2)

    for label, pattern, cmd in SCRIPTS:
        if label == 'whalebot' and os.path.exists('/root/w_paused'):
            log('whalebot paused (flag: /root/w_paused)')
            continue
        if not is_running(pattern):
            log('DEAD: ' + label + ' - restarting...')
            start_script(label, cmd)
            restarted.append(label)
            time.sleep(2)

    if restarted:
        log('Watchdog restarted: ' + ', '.join(restarted))
    else:
        log('All processes healthy (4 C-bots + 7 D-bots + scan16 + rotation_manager + dashboard + whalebot)')

    fcntl.flock(lock_file, fcntl.LOCK_UN)
    lock_file.close()

if __name__ == '__main__':
    main()
