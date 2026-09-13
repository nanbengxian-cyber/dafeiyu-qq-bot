"""Persistent optional conversation scheduler. No model calls or message sending."""
from __future__ import annotations
import hashlib
import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone, timedelta
from pathlib import Path


def tokens(text):
    result = set(re.findall(r'[a-z][a-z0-9_.-]{2,}', text.lower()))
    for run in re.findall(r'[\u3400-\u9fff]+', text):
        result.update(run[i:i+2] for i in range(len(run)-1))
    return result - {'这个','那个','然后','什么','怎么','一下','可以','就是','不是','你说','觉得','现在'}


def related(a, b):
    a, b = tokens(a), tokens(b)
    common = a & b
    return bool(len(common) >= 2 and len(common) / max(1, min(len(a), len(b))) >= .3)


class ActionStore:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as con, con:
            con.executescript('''
            CREATE TABLE IF NOT EXISTS action_groups(gid TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS action_seen(gid TEXT, eid TEXT, ts REAL, PRIMARY KEY(gid,eid));
            CREATE INDEX IF NOT EXISTS action_seen_ts ON action_seen(ts);
            ''')

    def connect(self):
        return sqlite3.connect(self.path, timeout=.3)

    def _load(self, con, gid):
        row = con.execute('SELECT payload FROM action_groups WHERE gid=?', (gid,)).fetchone()
        return json.loads(row[0]) if row else {'queue': [], 'sent': [], 'traffic': [], 'ignored': 0}

    def _save(self, con, gid, state):
        con.execute('INSERT OR REPLACE INTO action_groups VALUES(?,?)', (gid, json.dumps(state)))

    def consider(self, gid, uid, eid, text, now, *, eligible=True, forbidden=False,
                 day_max=12, cooldown=180, motive='interest'):
        """Reserve one candidate atomically. Actual sends alone open participation windows.

        forbidden revokes queued candidates/windows for this person (privacy/boundary).
        eligible is supplied by the existing interest classifier, never by queue matching.
        """
        with closing(self.connect()) as con, con:
            con.execute('BEGIN IMMEDIATE')
            s = self._load(con, gid)
            con.execute('DELETE FROM action_seen WHERE ts<?', (now-86400,))
            if not con.execute('INSERT OR IGNORE INTO action_seen VALUES(?,?,?)', (gid,eid,now)).rowcount:
                return None, 'duplicate'
            s['queue'] = [q for q in s['queue'] if q['expires'] > now]
            s['sent'] = [t for t in s['sent'] if t > now-86400]
            s['traffic'] = [t for t in s['traffic'] if t > now-600][-19:] + [now]
            if forbidden:
                s['queue'] = [q for q in s['queue'] if q['uid'] != uid]
                if s.get('window', {}).get('uid') == uid:
                    s.pop('window', None)
                self._save(con,gid,s)
                return None, 'boundary'
            w = s.get('window')
            engaged = bool(w and w['uid'] == uid and related(text, w['topic']) and now < w['expires'])
            if w and not w.get('settled') and now >= w['expires']:
                # Silence with no subsequent group activity is unknown, not rejection.
                if not w.get('engaged') and len([t for t in s['traffic'] if t > w['sent_at']]) >= 3:
                    s['ignored'] += 1
                w['settled'] = True
            if engaged:
                w['engaged'] = True
                s['ignored'] = 0
            deferred = next((q for q in s['queue'] if q['uid'] == uid and related(text,q['topic'])), None)
            if eligible:
                key = hashlib.sha256((uid+text).encode()).hexdigest()[:24]
                s['queue'] = [q for q in s['queue'] if q['key'] != key]
                s['queue'].append(dict(key=key, uid=uid, topic=text[:240], motive=motive,
                                       expires=now+600, created=now))
                s['queue'] = s['queue'][-8:]
            continuation = bool(engaged and w.get('replies',0) < 3)
            reason = ''
            p = s.get('pending')
            if p and p.get('expires', 0) > now:
                reason = 'pending'
            elif not eligible and not continuation:
                reason = 'no_opportunity'
            elif len(s['traffic']) < 3:
                reason = 'insufficient_context'
            elif len([t for t in s['sent'] if t > now-600]) >= 4:
                reason = 'density'
            elif now-s.get('last',-1e10) < (20 if continuation else cooldown):
                reason = 'cooldown'
            elif s['ignored'] >= 2 and now-s.get('last',0) < 1800:
                reason = 'withdraw'
            elif len([t for t in s['sent'] if t > now-300]) >= 2 and not continuation:
                reason = 'burst'
            day = datetime.fromtimestamp(now, timezone(timedelta(hours=8))).date().isoformat()
            if s.get('day') != day:
                s['day'], s['opens'] = day, 0
            if not reason and not continuation and s['opens'] >= day_max:
                reason = 'quota'
            if reason:
                self._save(con,gid,s)
                return None, reason
            token = hashlib.sha256((gid+':'+eid).encode()).hexdigest()[:32]
            action = dict(token=token, uid=uid, topic=text[:240], expires=now+120,
                          continuation=continuation, motive=('continuity' if continuation else
                          'deferred_interest' if deferred else motive))
            s['pending'] = action
            s['queue'] = [q for q in s['queue'] if not(q['uid']==uid and related(q['topic'],text))]
            self._save(con,gid,s)
            return action, 'reserved'

    def revoke(self, gid, uid):
        with closing(self.connect()) as con, con:
            con.execute('BEGIN IMMEDIATE')
            s = self._load(con, gid)
            s['queue'] = [q for q in s['queue'] if q['uid'] != uid]
            for key in ('pending', 'window'):
                if s.get(key, {}).get('uid') == uid:
                    s.pop(key, None)
            self._save(con, gid, s)

    def valid(self, gid, token, now):
        with closing(self.connect()) as con:
            p = self._load(con,gid).get('pending')
            return bool(p and p.get('token')==token and p.get('expires',0)>now)

    def release(self, gid, token):
        with closing(self.connect()) as con, con:
            con.execute('BEGIN IMMEDIATE')
            s = self._load(con,gid)
            if s.get('pending',{}).get('token') == token:
                s.pop('pending',None)
                self._save(con,gid,s)

    def sent(self, gid, token, now):
        with closing(self.connect()) as con, con:
            con.execute('BEGIN IMMEDIATE')
            s = self._load(con,gid)
            p = s.get('pending',{})
            if p.get('token') != token:
                return False
            s.pop('pending',None)
            if p.get('continuation') and s.get('window'):
                s['window']['replies'] += 1
            else:
                s['window'] = dict(uid=p.get('uid'),topic=p.get('topic'),sent_at=now,expires=now+150,
                                   replies=0,engaged=False,settled=False)
                s['opens'] = s.get('opens',0)+1
            s['last'] = now
            s['sent'].append(now)
            self._save(con,gid,s)
            return True

    def snapshot(self, gid):
        with closing(self.connect()) as con:
            return self._load(con,gid)
