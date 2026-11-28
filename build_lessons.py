#!/usr/bin/env python3
"""يبني lessons.json لتطبيق «زاد المسلم» من قنوات يوتيوب المذكورة في sources.json.

- قائمة التشغيل = سلسلة، وتُوزَّع على الأقسام بحسب كلمات عنوانها (أو بتخصيص منك).
- الفيديوهات التي ليست في أي قائمة تدخل «محاضرات منفردة» (الأحدث أولًا).
- تستعمل YouTube Data API الرسمي بمفتاح من المتغيّر YT_API_KEY. مكتبة بايثون القياسية فقط.
- إن فشل أي شيء لا تُكتب النتيجة، فيبقى آخر lessons.json سليم كما هو.
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
API = os.environ.get('YT_API_BASE', 'https://www.googleapis.com/youtube/v3').rstrip('/')
KEY = os.environ.get('YT_API_KEY', '').strip()
SRC = os.path.join(HERE, os.environ.get('SOURCES', 'sources.json'))
OUT = os.path.join(HERE, os.environ.get('OUT_JSON', 'lessons.json'))
REP = os.path.join(HERE, os.environ.get('OUT_REPORT', 'report.md'))
APP_MAX = 200000          # حدّ أمان فقط (التطبيق يقرأ الدروس كلها بلا حدّ عملي)
CALLS = {'n': 0}


class Fail(Exception):
    pass


def log(*a):
    print(*a, flush=True)


# ---------------------------------------------------------------- الاتصال بيوتيوب
def call(path, **p):
    p = {k: v for k, v in p.items() if v not in (None, '')}
    p['key'] = KEY
    url = '%s/%s?%s' % (API, path, urllib.parse.urlencode(p))
    for i in range(4):
        try:
            CALLS['n'] += 1
            with urllib.request.urlopen(url, timeout=40) as r:
                return json.loads(r.read().decode('utf8'))
        except urllib.error.HTTPError as e:
            raw = e.read().decode('utf8', 'replace')
            try:
                err = json.loads(raw)['error']
                reason = (err.get('errors') or [{}])[0].get('reason', '')
                msg = err.get('message', '')
            except Exception:
                reason, msg = '', raw[:200]
            if e.code >= 500 and i < 3:
                time.sleep(2 * (i + 1))
                continue
            if reason in ('quotaExceeded', 'dailyLimitExceeded', 'rateLimitExceeded'):
                raise Fail('انتهت الحصة اليومية لمفتاح يوتيوب. أعد التشغيل غدًا، أو قلّل «max_orphan_lectures» في sources.json.')
            if 'API key not valid' in msg or reason in ('keyInvalid', 'badRequest') and 'key' in msg.lower():
                raise Fail('مفتاح يوتيوب غير صحيح. راجع السر YT_API_KEY في إعدادات المستودع (Settings ← Secrets).')
            if reason in ('accessNotConfigured', 'SERVICE_DISABLED') or 'has not been used' in msg or 'is disabled' in msg:
                raise Fail('خدمة «YouTube Data API v3» غير مفعّلة في مشروع Google الخاص بالمفتاح. فعّلها من Library ثم أعد التشغيل.')
            raise Fail('رد يوتيوب بخطأ %s (%s): %s' % (e.code, reason, msg))
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if i < 3:
                time.sleep(2 * (i + 1))
                continue
            raise Fail('تعذّر الاتصال بيوتيوب: %s' % e)


def pages(path, **p):
    """يمرّ على كل صفحات النتائج ويُرجع العناصر تباعًا."""
    tok = None
    while True:
        d = call(path, maxResults=50, pageToken=tok, **p)
        for it in d.get('items', []):
            yield it
        tok = d.get('nextPageToken')
        if not tok:
            return


# ---------------------------------------------------------------- أدوات
def clean(t, n):
    return re.sub(r'\s+', ' ', t or '').strip()[:n]


_AR = str.maketrans({'أ': 'ا', 'إ': 'ا', 'آ': 'ا', 'ى': 'ي', 'ة': 'ه', 'ؤ': 'و', 'ئ': 'ي'})


def norm(t):
    t = re.sub(r'[ً-ْـ]', '', t or '')
    return t.translate(_AR).lower()


def secs(iso):
    m = re.fullmatch(r'P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?', iso or '')
    if not m:
        return 0
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def channel_id(ref):
    ref = ref.strip()
    m = re.search(r'(UC[\w-]{22})', ref)
    if m:
        return m.group(1)
    m = re.search(r'@([^/?#\s]+)', urllib.parse.unquote(ref))
    handle = '@' + m.group(1) if m else ('@' + ref if re.fullmatch(r'[\w.\-]+', ref) else '')
    if not handle:
        raise Fail('رابط القناة غير مفهوم: %s' % ref)
    d = call('channels', part='id', forHandle=handle)
    if not d.get('items'):
        raise Fail('لم أجد قناة بالاسم %s' % handle)
    return d['items'][0]['id']


def playlist_id(ref):
    m = re.search(r'(?:list=|^)((?:PL|UU|OL|FL|LL)[\w-]{10,})', ref.strip())
    if not m:
        raise Fail('رابط قائمة التشغيل غير مفهوم: %s' % ref)
    return m.group(1)


DET = {}


def details(ids):
    need = [i for i in dict.fromkeys(ids) if i not in DET]
    for k in range(0, len(need), 50):
        chunk = need[k:k + 50]
        got = {}
        for it in call('videos', part='contentDetails,status', id=','.join(chunk)).get('items', []):
            st = it.get('status', {})
            got[it['id']] = {'dur': secs(it.get('contentDetails', {}).get('duration')),
                             'emb': st.get('embeddable', True),
                             'pub': st.get('privacyStatus', 'public') == 'public'}
        for i in chunk:
            DET[i] = got.get(i)        # None = محذوف أو خاص


def playlist_items(pid):
    for it in pages('playlistItems', part='snippet,contentDetails', playlistId=pid):
        sn = it.get('snippet', {})
        vid = (sn.get('resourceId') or {}).get('videoId') or it.get('contentDetails', {}).get('videoId')
        t = sn.get('title', '')
        if not vid or t in ('Private video', 'Deleted video'):
            continue
        yield {'id': vid, 'title': t, 'at': it.get('contentDetails', {}).get('videoPublishedAt') or sn.get('publishedAt', '')}


def lesson(v, d):
    x = {'id': v['id'], 'title': clean(v['title'], 160), 'duration': d['dur'], 'yt': v['id']}
    if not d['emb']:
        x['noembed'] = True            # صاحب الفيديو يمنع تشغيله خارج يوتيوب
    return x


def recent(vs):
    lim = datetime.now(timezone.utc) - timedelta(days=30)
    for v in vs:
        try:
            if datetime.fromisoformat(v['at'].replace('Z', '+00:00')) > lim:
                return True
        except Exception:
            pass
    return False


# ---------------------------------------------------------------- الإعداد
def load_cfg():
    try:
        with open(SRC, encoding='utf8') as f:
            c = json.load(f)
    except FileNotFoundError:
        raise Fail('ملف sources.json غير موجود.')
    except json.JSONDecodeError as e:
        raise Fail('sources.json فيه خطأ في الصياغة عند السطر %d العمود %d: %s (غالبًا فاصلة ناقصة أو زائدة)' % (e.lineno, e.colno, e.msg))
    if not isinstance(c.get('sections'), list) or not c['sections']:
        raise Fail('sources.json: لا توجد «sections».')
    ids = [s.get('id') for s in c['sections']]
    if any(not i or not s.get('title') for i, s in zip(ids, c['sections'])) or len(set(ids)) != len(ids):
        raise Fail('sources.json: كل قسم يحتاج «id» و«title» وبلا تكرار في id.')
    c.setdefault('default_section', ids[-1])
    if c['default_section'] not in ids:
        raise Fail('sources.json: default_section غير موجود بين الأقسام.')
    c.setdefault('max_orphan_lectures', 150)
    c.setdefault('min_lecture_seconds', 120)
    c.setdefault('playlist_section', {})
    for k, v in c['playlist_section'].items():
        if v not in ids:
            raise Fail('sources.json: playlist_section يشير إلى قسم غير موجود: %s' % v)
    if not isinstance(c.get('sheikhs'), list) or not c['sheikhs']:
        raise Fail('sources.json: لا يوجد «sheikhs».')
    seen = set()
    for sh in c['sheikhs']:
        if not sh.get('id') or not sh.get('name'):
            raise Fail('sources.json: كل شيخ يحتاج «id» و«name».')
        if sh['id'] in seen:
            raise Fail('sources.json: معرّف شيخ مكرر: %s' % sh['id'])
        seen.add(sh['id'])
        for k in ('playlists', 'skip'):
            if sh.get(k) is not None and not isinstance(sh[k], list):
                raise Fail('sources.json: «%s» عند %s لازم تكون قائمة بين أقواس مربعة، مثل ["رابط1", "رابط2"].' % (k, sh['name']))
        for k in ('section', 'lectures_section'):
            if sh.get(k) and sh[k] not in ids:
                raise Fail('sources.json: «%s» عند %s يشير إلى قسم غير موجود: %s' % (k, sh['name'], sh[k]))
    return c


def pick_section(cfg, sh, pid, title):
    ov = cfg['playlist_section']
    if pid in ov:
        return ov[pid], 'مخصّص'
    t = norm(title)
    for s in cfg['sections']:
        if any(w and norm(w) in t for w in s.get('words', [])):
            return s['id'], 'كلمة مفتاحية'
    return (sh.get('section') or cfg['default_section']), 'افتراضي'


# ---------------------------------------------------------------- البناء
def build(cfg):
    buckets = {s['id']: {} for s in cfg['sections']}          # قسم ← شيخ ← {series, lectures}
    rep = []
    sections_by_id = {s['id']: s for s in cfg['sections']}
    for sh in cfg['sheikhs']:
        ref = (sh.get('channel') or '').strip()
        explicit = sh.get('playlists') or []
        if not ref and not explicit:
            log('تخطّي «%s»: لا يوجد رابط قناة ولا قوائم محددة بعد.' % sh['name'])
            continue
        log('— %s' % sh['name'])
        cid = channel_id(ref) if ref else None
        # بلا «playlists»: كل قوائم القناة (كما كان). مع «playlists»: هذه القوائم فقط، إلا لو كتبت "scan_channel": true فتُقرأ معها كل قوائم القناة.
        scan = bool(sh.get('scan_channel')) or not explicit
        if scan and not cid:
            log('   تنبيه: «scan_channel» يحتاج رابط قناة؛ سأقرأ القوائم المحددة فقط.')
            scan = False
        skip = {playlist_id(x) for x in (sh.get('skip') or [])}
        pls = []
        if explicit:
            ids = [playlist_id(x) for x in explicit]
            for k in range(0, len(ids), 50):
                for it in call('playlists', part='snippet', id=','.join(ids[k:k + 50])).get('items', []):
                    pls.append({'id': it['id'], 'title': it['snippet']['title']})
            missing = set(ids) - {p['id'] for p in pls}
            if missing:
                raise Fail('قوائم غير موجودة أو خاصة عند %s: %s' % (sh['name'], ' '.join(sorted(missing))))
        if scan:
            have = {p['id'] for p in pls}              # القائمة المحددة التي هي أيضًا على القناة لا تتكرر
            pls += [{'id': it['id'], 'title': it['snippet']['title']} for it in pages('playlists', part='snippet', channelId=cid) if it['id'] not in have]
        lost = skip - {p['id'] for p in pls}
        if lost and scan:
            log('   تنبيه: قوائم في «skip» غير موجودة في القناة: %s' % ' '.join(sorted(lost)))
        n_lec = sh.get('lectures')
        if n_lec is None:
            n_lec = cfg['max_orphan_lectures'] if scan else 0
        if n_lec and not cid:
            log('   تنبيه: المحاضرات المنفردة تحتاج رابط قناة؛ تخطّيتها.')
            n_lec = 0
        in_pl = set()
        got_series = 0
        for pl in pls:
            vs = list(playlist_items(pl['id']))
            in_pl.update(v['id'] for v in vs)
            if pl['id'] in skip or not vs:
                continue
            details([v['id'] for v in vs])
            ls = [lesson(v, DET[v['id']]) for v in vs if DET.get(v['id']) and DET[v['id']]['pub'] and DET[v['id']]['dur'] > 0]
            if not ls:
                continue
            sid, why = pick_section(cfg, sh, pl['id'], pl['title'])
            ser = {'id': pl['id'], 'title': clean(pl['title'], 120), 'lessons': ls}
            if recent(vs):
                ser['new'] = True
            buckets[sid].setdefault(sh['id'], {'sh': sh, 'series': [], 'lectures': []})['series'].append(ser)
            rep.append((sh['name'], sections_by_id[sid]['title'], ser['title'], len(ls), pl['id'], why))
            got_series += 1
        got_lec = 0
        if n_lec:
            lecs = []
            for v in playlist_items('UU' + cid[2:]):       # قائمة «التحميلات» = كل فيديوهات القناة، الأحدث أولًا
                if v['id'] in in_pl:
                    continue
                details([v['id']])
                d = DET.get(v['id'])
                if d and d['pub'] and d['dur'] >= cfg['min_lecture_seconds']:
                    lecs.append(lesson(v, d))
                    if len(lecs) >= n_lec:
                        break
            if lecs:
                sid = sh.get('lectures_section') or sh.get('section') or cfg['default_section']
                buckets[sid].setdefault(sh['id'], {'sh': sh, 'series': [], 'lectures': []})['lectures'] = lecs
                got_lec = len(lecs)
        log('   %d سلسلة، %d محاضرة منفردة' % (got_series, got_lec))
    out = {'title': clean(cfg.get('title'), 60) or 'الدروس والمحاضرات', 'sections': []}
    total = 0
    for s in cfg['sections']:
        b = buckets[s['id']]
        shks = []
        for sh in cfg['sheikhs']:
            e = b.get(sh['id'])
            if not e:
                continue
            x = {'id': sh['id'], 'name': clean(sh['name'], 80), 'bio': clean(sh.get('bio'), 200)}
            if any(se.get('new') for se in e['series']):
                x['new'] = True
            if e['series']:
                x['series'] = e['series']
            if e['lectures']:
                x['lectures'] = e['lectures']
            total += sum(len(se['lessons']) for se in e['series']) + len(e['lectures'])
            shks.append(x)
        if shks:
            sec = {'id': s['id'], 'title': clean(s['title'], 60), 'shaykhs': shks}
            if s.get('icon'):
                sec['icon'] = s['icon']
            out['sections'].append(sec)
    if not out['sections']:
        raise Fail('لم أجد أي درس. تأكد من روابط القنوات في sources.json.')
    return out, rep, total


def write_report(rep, total, out, cfg):
    L = ['# ملخص آخر تحديث', '', 'إجمالي الدروس: **%d**' % total, '']
    for sec in out['sections']:
        L += ['## ' + sec['title'], '']
        for sh in sec['shaykhs']:
            nl = len(sh.get('lectures', []))
            L.append('- **%s**: %d سلسلة%s' % (sh['name'], len(sh.get('series', [])), ('، و%d محاضرة منفردة' % nl) if nl else ''))
        L.append('')
    L += ['## القوائم وأقسامها', '', 'لتغيير قسم قائمة: أضف معرّفها وقسمها في «playlist_section» داخل sources.json.', '',
          '| الشيخ | القسم | السلسلة | الدروس | معرّف القائمة | كيف اختير القسم |', '|---|---|---|---|---|---|']
    for r in rep:
        L.append('| %s | %s | %s | %d | `%s` | %s |' % (r[0], r[1], r[2].replace('|', '/'), r[3], r[4], r[5]))
    odd = [r for r in rep if r[5] == 'افتراضي']
    if odd:
        L += ['', '## قوائم لم يُعرف قسمها (وُضعت في القسم الافتراضي)', '']
        L += ['- %s — «%s» (`%s`)' % (r[0], r[2], r[4]) for r in odd]
    with open(REP, 'w', encoding='utf8') as f:
        f.write('\n'.join(L) + '\n')


def main():
    if not KEY:
        raise Fail('السر YT_API_KEY غير مضبوط. أضفه من Settings ← Secrets and variables ← Actions.')
    cfg = load_cfg()
    out, rep, total = build(cfg)
    repo = os.environ.get('GITHUB_REPOSITORY', '')
    if '/' in repo and os.path.exists(os.path.join(HERE, 'player.html')):      # صفحة المشغّل المستضافة (GitHub Pages) لتفادي خطأ يوتيوب 153
        o, r = repo.split('/', 1)
        out['player'] = 'https://%s.github.io/%s/player.html' % (o.lower(), r)
    if total > APP_MAX:
        log('تنبيه: الإجمالي %d أكبر من %d، والتطبيق حاليًا يقرأ %d فقط. قلّل المحاضرات المنفردة أو القوائم.' % (total, APP_MAX, APP_MAX))
    data = json.dumps(out, ensure_ascii=False, separators=(',', ':'))
    tmp = OUT + '.tmp'
    with open(tmp, 'w', encoding='utf8') as f:
        f.write(data)
    os.replace(tmp, OUT)
    write_report(rep, total, out, cfg)
    log('تم: %d درسًا في %d قسمًا (%d طلب إلى يوتيوب، حجم الملف %d كيلوبايت)' % (total, len(out['sections']), CALLS['n'], len(data.encode('utf8')) // 1024))


if __name__ == '__main__':
    try:
        main()
    except Fail as e:
        log('خطأ: %s' % e)
        sys.exit(1)
