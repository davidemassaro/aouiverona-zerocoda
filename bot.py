"""Bot Telegram per ULSS 9 Scaligera: prenota o monitora uno slot (e lo prenota quando si libera).

Configurazione in .env (vedi .env.example): TELEGRAM_TOKEN, LOG_LEVEL (DEBUG|INFO|WARNING|ERROR), POLL_SECONDS, FAKE_EMAIL, FAKE_PHONE
e, solo per creare utenti.json la prima volta, ADMIN_ID.
  utenti.json    admin, utenti autorizzati con profilo (mail/telefono reali) e monitoraggi attivi

Il monitoraggio usa credenziali fittizie; mail/telefono reali dell'utente vengono usati solo
nel momento della prenotazione.
"""
import calendar
import importlib
import json
import logging
import os
import random
import re
import time
from datetime import datetime, timedelta

import requests

sc = importlib.import_module('check-scaligera')

BASE = os.path.dirname(os.path.abspath(__file__))


def carica_env(path):
    try:
        with open(path, encoding='utf-8') as f:
            for riga in f:
                k, sep, v = riga.strip().partition('=')
                if sep and not k.startswith('#'):
                    os.environ.setdefault(k.strip(), v.strip().strip('"\''))
    except FileNotFoundError:
        pass


def leggi(path, default):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def scrivi(path, dati):
    with open(path + '.tmp', 'w', encoding='utf-8') as f:  # scrittura atomica: niente file troncati
        json.dump(dati, f, indent=1, ensure_ascii=False)
    os.replace(path + '.tmp', path)


carica_env(os.path.join(BASE, '.env'))
LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO').upper()  # DEBUG | INFO | WARNING | ERROR
logging.basicConfig(level=LOG_LEVEL if LOG_LEVEL in ('DEBUG', 'INFO', 'WARNING', 'ERROR') else 'INFO',
                    format='%(asctime)s %(levelname)-7s %(message)s')
logging.getLogger('urllib3').setLevel(logging.WARNING)  # a DEBUG scriverebbe nel log gli URL con il token del bot
log = logging.getLogger('bot')
DEBUG = log.isEnabledFor(logging.DEBUG)  # traceback completi solo in debug
TOKEN = os.environ.get('TELEGRAM_TOKEN')
if not TOKEN:
    raise SystemExit('Manca TELEGRAM_TOKEN: copia .env.example in .env e compilalo.')
POLL = int(os.environ.get('POLL_SECONDS', 120))
MODO = os.environ.get('BOT_MODE', 'private').strip().lower()  # private: serve l'ok dell'admin | public: accetta tutti
if MODO not in ('private', 'public'):
    raise SystemExit(f'BOT_MODE non valido: "{MODO}" (usa private o public)')
FAKE_EMAIL = os.environ.get('FAKE_EMAIL', 'monitor.slot@example.com')
FAKE_PHONE = os.environ.get('FAKE_PHONE', '3000000000')
UTENTI_FILE = os.path.join(BASE, 'utenti.json')
API = f'https://api.telegram.org/bot{TOKEN}/'

# {"admins": [uid], "utenti": {"uid": {"email": ..., "tel": ...}}, "jobs": [...], "next_id": n}
# utente autorizzato = presente in "utenti"
UT = leggi(UTENTI_FILE, None)
if UT is None:
    if not os.environ.get('ADMIN_ID'):
        raise SystemExit('Manca utenti.json: imposta ADMIN_ID in .env per crearlo.')
    UT = {'admins': [int(os.environ['ADMIN_ID'])], 'utenti': {}}
    scrivi(UTENTI_FILE, UT)
UT.setdefault('jobs', [])
UT.setdefault('next_id', 1)
CONV = {}  # uid -> conversazione in corso (solo in memoria)


MTIME = 0  # data dell'ultima versione di utenti.json letta o scritta dal bot


def salva():
    global MTIME
    scrivi(UTENTI_FILE, UT)
    MTIME = os.path.getmtime(UTENTI_FILE)


def ricarica():
    """Rilegge utenti.json se e' stato modificato a mano mentre il bot gira."""
    global MTIME
    try:
        m = os.path.getmtime(UTENTI_FILE)
        if m == MTIME:
            return
        nuovo = leggi(UTENTI_FILE, None)
        if nuovo is None:
            return
    except (OSError, ValueError) as e:  # file a meta' o JSON errato: si tiene lo stato in memoria
        log.warning('utenti.json non leggibile, riprovo: %r', e)
        return
    nuovo.setdefault('jobs', [])
    nuovo.setdefault('next_id', 1)
    UT.clear()
    UT.update(nuovo)
    MTIME = m
    log.info('utenti.json ricaricato (%d utenti, %d monitoraggi)', len(UT['utenti']), len(UT['jobs']))


def profilo(uid):
    p = UT['utenti'].get(str(uid)) or {}
    return p if p.get('email') else None


def tg(metodo, **p):
    log.debug('telegram %s', metodo)
    r = requests.post(API + metodo, json=p, timeout=40).json()
    if not r.get('ok'):
        log.error('telegram %s: %s', metodo, r.get('description'))
    return r.get('result')


def say(chat, text, opts=None, extra=None):
    """Messaggio; con `opts` mostra un bottone per opzione, piu' extra [(testo, callback)]."""
    righe = [[{'text': o[1][:60], 'callback_data': str(k)}] for k, o in enumerate(opts or [])]
    righe += [[{'text': t, 'callback_data': c}] for t, c in (extra or [])]
    return tg('sendMessage', chat_id=chat, text=text, **({'reply_markup': {'inline_keyboard': righe}} if righe else {}))


def autorizzato(uid):
    return MODO == 'public' or uid in UT['admins'] or str(uid) in UT['utenti']


# ---------- conversazione ----------
def mostra(chat, st):
    say(chat, f"Scegli {sc.LIVELLI[st['n']][2]}:" if st['opts'] else 'Nessuna disponibilita\'.',
        st['opts'], [('« Indietro', 'b'), ('Annulla', 'x')] if st['n'] else [('Annulla', 'x')])


def libera(uid):
    return CONV.pop(uid, None)


def leggi_riepilogo(sess, testo):
    """Legge il riepilogo e rilascia SUBITO il posto: mentre l'utente decide non resta nulla bloccato sul portale."""
    try:
        return sc.riepilogo(testo)
    finally:
        sc.annulla(sess, testo)


NOTA = "\n\nL'orario esatto lo assegna il portale al momento della conferma e puo' cambiare; se lo slot nel frattempo non e' piu' disponibile te lo dico."


def inizia(chat, uid, mode):
    libera(uid)
    CONV[uid] = {'mode': mode, 'chat': chat, 'scelte': []}
    if not profilo(uid):
        CONV[uid]['step'] = 'email'
        return say(chat, 'Prima volta: salvo il tuo profilo (usato solo per la conferma finale).\nLa tua email:', None, [('Annulla', 'x')])
    avvia(chat, uid)


def avvia(chat, uid):
    st = CONV[uid]  # la navigazione usa sempre credenziali fittizie
    st['sess'], st['opts'] = sc.login(FAKE_EMAIL, FAKE_PHONE)
    st.update(step='pick', n=0)
    mostra(chat, st)


def testo_utente(uid, chat, t):
    st = CONV.get(uid)
    log.debug('testo uid=%s step=%s', uid, st and st.get('step'))  # il testo puo' contenere mail/telefono: non lo loggo
    if not st:
        return say(chat, 'Usa /prenota o /monitora.')
    s = st['step']
    if s == 'email':
        if not re.fullmatch(r'[^@\s]+@[^@\s]+\.\w+', t):
            return say(chat, 'Email non valida, riprova:', None, [('Annulla', 'x')])
        st['email'], st['step'] = t, 'tel'
        say(chat, 'Il tuo telefono (senza prefisso, es. 3331234567):', None, [('Annulla', 'x')])
    elif s == 'tel':
        tel = re.sub(r'^\+?39|[\s.-]', '', t)
        if not re.fullmatch(r'\d{9,10}', tel):
            return say(chat, 'Telefono non valido, riprova:', None, [('Annulla', 'x')])
        UT['utenti'][str(uid)] = {'email': st['email'], 'tel': tel}
        salva()
        if st['mode']:
            say(chat, 'Profilo salvato.')
            avvia(chat, uid)
        else:
            del CONV[uid]
            say(chat, 'Profilo aggiornato.')
    elif s in ('data', 'fino'):
        try:
            if s == 'fino' and t.lower() in ('uguale', 'stessa'):
                d = st['data']
            elif s == 'fino' and t.lower() in ('mai', 'illimitata', 'nessuna'):
                d = None
            elif s == 'data' and t.lower() == 'oggi':
                d = datetime.now().strftime('%Y%m%d')
            else:
                m = re.fullmatch(r'(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})', t)
                d = datetime(int(m[3]), int(m[2]), int(m[1])).strftime('%Y%m%d')
        except (TypeError, ValueError):
            return say(chat, 'Formato non valido, scrivi GG/MM/AAAA:', None, [('Annulla', 'x')])
        st[s] = d
        if s == 'data':
            st['step'] = 'fino'
            return datepicker(chat, *FINE)
        st['step'] = 'ora'
        say(chat, 'Dalle che ora? (es. 08:00) o "tutti":', None, [('« Indietro', 'b'), ('Annulla', 'x')])
    elif s in ('ora', 'ora_a'):
        if s == 'ora' and t.lower() in ('tutti', 'tutte', 'qualsiasi'):
            st['ora'] = st['ora_a'] = None
        else:
            m = re.fullmatch(r'(\d{1,2})[:.](\d{2})', t)
            if not m or int(m[1]) > 23 or int(m[2]) > 59:
                return say(chat, 'Formato non valido. Scrivi HH:MM' + (' o "tutti"' if s == 'ora' else '') + ':',
                           None, [('« Indietro', 'b'), ('Annulla', 'x')])
            st[s] = f'{int(m[1]):02d}:{m[2]}'
            if s == 'ora':
                st['step'] = 'ora_a'
                return say(chat, f"Fino a che ora? Ti prenoto solo uno slot che inizia tra le {st['ora']} e l'ora che scrivi (es. {st['ora']} per un solo slot):",
                           None, [('« Indietro', 'b'), ('Annulla', 'x')])
            if st['ora_a'] < st['ora']:
                return say(chat, f"L'ora finale deve essere dopo le {st['ora']}:", None, [('« Indietro', 'b'), ('Annulla', 'x')])
        st['step'] = 'modo'
        say(chat, 'Quando trovo uno slot che rispetta i criteri, come procedo?', None,
            [('Prenota in automatico', 'o:auto'), ('Avvisami e confermo io', 'o:avvisa'),
             ('« Indietro', 'b'), ('Annulla', 'x')])


def crea_job(uid, chat, st, auto):
    UT['jobs'].append({
        'id': UT['next_id'], 'uid': uid, 'chat': chat,
        'sede': st['scelte'][0], 'servizio': st['scelte'][1], 'data': st['data'], 'fino': st['fino'],
        'ora': st['ora'], 'ora_a': st['ora_a'], 'auto': auto, 'notificati': [], 'next': 0})
    UT['next_id'] += 1
    salva()
    del CONV[uid]
    log.info('monitoraggio #%s creato uid=%s: %s', UT['next_id'] - 1, uid, descr(UT['jobs'][-1]))
    say(chat, f"Monitoraggio #{UT['next_id'] - 1} attivo: {descr(UT['jobs'][-1])}\n"
              f"Controllo ogni ~{POLL}s con credenziali fittizie; "
              + ('prenoto da solo col tuo profilo appena si libera.' if auto else 'ti avviso appena trovo slot e confermi tu.'))


MESI = ['Gennaio', 'Febbraio', 'Marzo', 'Aprile', 'Maggio', 'Giugno', 'Luglio', 'Agosto', 'Settembre',
        'Ottobre', 'Novembre', 'Dicembre']


def cal_kb(y, m, extra):
    """Tastiera calendario: 'd:AAAAMMGG' = giorno scelto, 'm:AAAAMM' = cambio mese, 'n' = niente."""
    nop = lambda t: {'text': t, 'callback_data': 'n'}
    righe = [[nop(f'{MESI[m - 1]} {y}')], [nop(g) for g in 'LMMGVSD']]
    for sett in calendar.Calendar().monthdayscalendar(y, m):
        righe.append([{'text': str(d), 'callback_data': f'd:{y}{m:02d}{d:02d}'} if d else nop(' ') for d in sett])
    py, pm = (y, m - 1) if m > 1 else (y - 1, 12)
    ny, nm = (y, m + 1) if m < 12 else (y + 1, 1)
    righe.append([{'text': '<', 'callback_data': f'm:{py}{pm:02d}'}, {'text': '>', 'callback_data': f'm:{ny}{nm:02d}'}])
    return {'inline_keyboard': righe + [[{'text': t, 'callback_data': c}] for t, c in extra + [('« Indietro', 'b'), ('Annulla', 'x')]]}


INIZIO = ('Inizio finestra: scegli il giorno (o scrivi GG/MM/AAAA):', [('Oggi', 'd:oggi')])
FINE = ('Fine finestra: scegli il giorno (o scrivi GG/MM/AAAA):',
        [('Solo quel giorno', 'd:uguale'), ('Nessun limite', 'd:mai')])


def indietro(uid, chat, st):
    """Torna al passo precedente rifacendo il percorso (con dati fittizi) fino al livello giusto."""
    s = st['step']
    if s == 'modo' and st['ora']:
        st['step'] = 'ora_a'
        return say(chat, 'Fino a che ora? (stesso orario per un solo slot):', None, [('« Indietro', 'b'), ('Annulla', 'x')])
    if s in ('ora_a', 'modo'):
        st['step'] = 'ora'
        return say(chat, 'Dalle che ora? (es. 08:00) o "tutti":', None, [('« Indietro', 'b'), ('Annulla', 'x')])
    if s in ('ora', 'fino'):
        st['step'] = 'fino' if s == 'ora' else 'data'
        return datepicker(chat, *(FINE if s == 'ora' else INIZIO))
    if s == 'conferma':
        k = 3
    elif s == 'data':
        k = 1
    elif s == 'pick' and st['n']:
        k = st['n'] - 1
    else:
        return
    st['scelte'] = st['scelte'][:k]
    st['sess'], st['opts'] = sc.login(FAKE_EMAIL, FAKE_PHONE)
    _, st['opts'] = percorri(st['sess'], st['opts'], st['scelte'])
    if k == 1 and len(st['opts']) == 1:  # servizio unico, saltato in avanti: salto anche all'indietro
        k, st['scelte'] = 0, []
        st['sess'], st['opts'] = sc.login(FAKE_EMAIL, FAKE_PHONE)
    st.update(step='pick', n=k)
    mostra(chat, st)


def datepicker(chat, testo, extra):
    oggi = datetime.now()
    tg('sendMessage', chat_id=chat, text=testo, reply_markup=cal_kb(oggi.year, oggi.month, extra))


def modifica_profilo(uid, chat, testo='Nuova email:'):
    libera(uid)
    CONV[uid] = {'mode': None, 'chat': chat, 'step': 'email'}
    say(chat, testo, None, [('Annulla', 'x')])


def callback(uid, chat, dati, mid=None):
    if dati == 'mod':
        return modifica_profilo(uid, chat)
    if dati[:2] in ('a:', 'r:'):
        return decidi_accesso(uid, chat, dati)
    if dati.startswith('s:'):
        return ferma_job(uid, chat, dati)
    if dati.startswith('p:'):
        return conferma_slot(uid, chat, dati)
    st = CONV.get(uid)
    if not st or dati == 'n':
        return
    if st['step'] == 'modo' and dati in ('o:auto', 'o:avvisa'):
        return crea_job(uid, chat, st, dati == 'o:auto')
    if dati == 'b':
        return indietro(uid, chat, st)
    if st['step'] in ('data', 'fino') and dati[:2] in ('d:', 'm:'):
        v = dati[2:]
        if dati[0] == 'm':  # cambio mese: stessa tastiera, stesso messaggio
            extra = (INIZIO if st['step'] == 'data' else FINE)[1]
            return tg('editMessageReplyMarkup', chat_id=chat, message_id=mid,
                      reply_markup=cal_kb(int(v[:4]), int(v[4:]), extra))
        return testo_utente(uid, chat, v if not v.isdigit() else f'{v[6:]}/{v[4:6]}/{v[:4]}')
    if dati == 'x':
        del CONV[uid]
        say(chat, 'Annullato.')
        return menu(uid, chat)
    if st['step'] == 'conferma' and dati == 'ok':
        del CONV[uid]
        try:  # solo ora si usano i dati reali: percorso completo e conferma immediata
            esito = prenota_reale(uid, st['scelte'])
        except Exception as e:
            say(chat, "Orario non piu' disponibile" + (f' ({e})' if str(e) else '') + '.')
            return menu(uid, chat)
        log.info('PRENOTATO uid=%s', uid)
        UT['jobs'] = [j for j in UT['jobs'] if j['id'] != st.get('job')]  # prenotato da un avviso: monitoraggio concluso
        salva()
        return say(chat, 'PRENOTATO: ' + esito)
    if st['step'] != 'pick':
        return
    scegli_opzione(uid, chat, st, st['opts'][int(dati)])


def scegli_opzione(uid, chat, st, scelta):
    n = st['n']
    st['scelte'].append(scelta[0])
    if st['mode'] == 'monitora' and n == 1:  # sede + servizio bastano, data/ora si scrivono
        st['step'] = 'data'
        return datepicker(chat, *INIZIO)
    st['testo'], st['opts'] = sc.avanza(st['sess'], n, scelta)
    st['n'] = n + 1
    if st['n'] == 1 and len(st['opts']) == 1:  # un solo servizio: inutile chiedere, lo scelgo io
        say(chat, f"Servizio: {st['opts'][0][1]} (unico disponibile)")
        return scegli_opzione(uid, chat, st, st['opts'][0])
    if n == 3:  # orario scelto -> riepilogo (letto e subito rilasciato, la prenotazione vera avviene alla conferma)
        st['step'] = 'conferma'
        return say(chat, 'Vuoi davvero confermare questa prenotazione?\n\n' + leggi_riepilogo(st['sess'], st['testo']) + NOTA, None,
                   [('Conferma prenotazione', 'ok'), ('« Indietro', 'b'), ('Annulla', 'x')])
    mostra(chat, st)


# ---------- monitoraggio ----------
def fascia(j):
    da, a = j['ora'], j.get('ora_a', j['ora'])  # j senza ora_a (vecchi monitoraggi): un solo orario
    return f'{da}-{a}' if da else 'qualsiasi'


def descr(j):
    return f"{j['sede']} / {j['servizio']} / dal {j['data']} al {j.get('fino', j['data']) or 'senza limite'} / ore {fascia(j)} / {'auto' if j.get('auto', True) else 'avviso'}"


def trova(opts, id_):
    return next(o for o in opts if o[0] == id_)


def controlla(j):
    """Cerca uno slot che combacia (con credenziali fittizie); se c'e', prenota con quelle reali."""
    sess, opts = sc.login(FAKE_EMAIL, FAKE_PHONE)
    _, opts = sc.avanza(sess, 0, trova(opts, j['sede']))
    _, date = sc.avanza(sess, 1, trova(opts, j['servizio']))
    # finestra [da, fino]: cerca_da restituisce 3 date per volta, si pagina finche' si esce dalla finestra
    da, fino = j['data'], j.get('fino', j['data'])
    cur, date = da, []
    for _ in range(40):
        pagina = [d for d in sc.cerca_da(sess, cur) if not fino or d[0] <= fino]
        date += [d for d in pagina if d not in date]
        if len(pagina) < 3:
            break
        cur = (datetime.strptime(pagina[-1][0], '%Y%m%d') + timedelta(days=1)).strftime('%Y%m%d')
    log.debug('job %s: date trovate: %s', j['id'], ', '.join(d[0] for d in date) or 'nessuna')
    trovati = []  # [(data, slot, orario reale o None)]
    for d in date:
        _, orari = sc.avanza(sess, 2, d)
        log.debug('job %s: %s orari: %s', j['id'], d[0], ', '.join(o[1] for o in orari) or 'nessuno')
        for o in orari:
            if j['ora'] and not slot_possibile(j, o[1]):
                log.debug('job %s: %s %s scartato (fuori fascia)', j['id'], d[0], o[1])
                continue
            # l'orario vero (es. 07:35) si vede solo nel riepilogo: lo leggo e rilascio subito il posto
            if j['ora'] or not j.get('auto', True):
                try:
                    reale = ora_reale([j['sede'], j['servizio'], d[0], o[0]])
                except Exception as e:
                    reale = None
                    log.warning('job %s: %s %s: lettura orario reale fallita: %r', j['id'], d[0], o[1], e)
                log.debug('job %s: %s %s orario reale %s', j['id'], d[0], o[1], reale)
                if reale is None:  # non verificabile: lo tengo, la fascia viene ricontrollata prima di prenotare
                    log.warning('job %s: %s %s orario reale non leggibile, slot tenuto', j['id'], d[0], o[1])
                elif j['ora'] and not in_fascia(j, reale):
                    log.debug('job %s: %s %s scartato (orario reale %s fuori fascia %s)', j['id'], d[0], o[1], reale, fascia(j))
                    continue
            else:
                reale = None
            trovati.append((d, o, reale))
    log.debug('job %s: %d date nella finestra, %d slot validi: %s', j['id'], len(date), len(trovati),
              ', '.join(f"{d[0]} {o[1]} (ore {reale or '?'})" for d, o, reale in trovati) or 'nessuno')
    if not trovati:
        j['notificati'] = []
        return False
    if j.get('auto', True):
        for d, o, _ in trovati:
            try:
                esito = prenota_reale(j['uid'], [j['sede'], j['servizio'], d[0], o[0]], j)
            except RuntimeError as e:  # orario spostato fuori fascia o slot preso: provo il prossimo
                log.warning('job %s: %s', j['id'], e)
                continue
            log.info('job %s: PRENOTATO %s %s', j['id'], d[0], o[0])
            say(j['chat'], f"Monitoraggio #{j['id']}: slot libero, PRENOTATO!\n{esito}")
            return True
        return False
    # modalita' avviso: un messaggio solo per gli slot non ancora notificati, la conferma e' dell'utente
    chiavi = [f'{d[0]}|{o[0]}' for d, o, _ in trovati]
    vecchi = j.get('notificati', [])
    nuovi = [t for t, k in zip(trovati, chiavi) if k not in vecchi]
    if not nuovi:
        log.debug('job %s: slot gia\' notificati, nessun nuovo messaggio', j['id'])
        j['notificati'] = chiavi
        return False
    log.info('job %s: avviso con %d slot nuovi', j['id'], len(nuovi))
    inviato = say(j['chat'], f"Monitoraggio #{j['id']}: slot disponibili! Premi per prenotare.", None,
                  [(f"{d[1]} ore {reale or o[1]}", f"p:{j['id']}:{d[0]}:{o[0]}") for d, o, reale in nuovi[:20]])
    # si segna "notificato" solo se Telegram ha accettato il messaggio, altrimenti si riprova al giro dopo
    j['notificati'] = chiavi if inviato else [k for k in chiavi if k in vecchi]
    if not inviato:
        log.error('job %s: avviso non consegnato, riprovo al prossimo controllo', j['id'])
    return False


def ora_reale(ids):
    """Orario vero dello slot [sede, servizio, data, orario]. Sessione nuova per ogni slot: dopo l'annulla
    il portale azzera il percorso e risponde con una pagina d'errore. Il posto viene sempre rilasciato."""
    sess, opts = sc.login(FAKE_EMAIL, FAKE_PHONE)
    testo, _ = percorri(sess, opts, ids)
    try:
        return sc.ora_riepilogo(testo)
    finally:
        sc.annulla(sess, testo)


def slot_possibile(j, etichetta):
    """Pre-filtro sull'etichetta '07:30 - 08:30': lo slot puo' contenere un orario nella fascia dell'utente."""
    inizio, fine = etichetta[:5], etichetta[8:13]
    return fine > j['ora'] and inizio <= j.get('ora_a', j['ora'])


def in_fascia(j, ora):
    return bool(ora) and j['ora'] <= ora <= j.get('ora_a', j['ora'])


def conferma_slot(uid, chat, dati):
    """Bottone di un avviso: mostra il riepilogo (con l'orario vero) e chiede conferma prima di prenotare."""
    _, jid, data, orario = dati.split(':', 3)
    j = next((j for j in UT['jobs'] if str(j['id']) == jid and j['uid'] == uid), None)
    if not j:
        return say(chat, 'Monitoraggio non piu\' attivo (gia\' prenotato o fermato).')
    ids = [j['sede'], j['servizio'], data, orario]
    try:  # navigazione con dati fittizi, come in /prenota; i reali si usano solo alla conferma
        sess, opts = sc.login(FAKE_EMAIL, FAKE_PHONE)
        testo, _ = percorri(sess, opts, ids)
    except Exception:
        return say(chat, 'Slot non piu\' disponibile. Il monitoraggio resta attivo.')
    CONV[uid] = {'mode': 'avviso', 'chat': chat, 'step': 'conferma', 'scelte': ids, 'job': j['id']}
    say(chat, 'Vuoi davvero confermare questa prenotazione?\n\n' + leggi_riepilogo(sess, testo) + NOTA, None,
        [('Conferma prenotazione', 'ok'), ('Annulla', 'x')])


def percorri(sess, opts, ids):
    """Da `opts` (sedi) segue gli id [sede, servizio, ...]. Ritorna (ultimo testo, opzioni del livello dopo)."""
    testo = None
    for n, id_ in enumerate(ids):
        if n == 2:  # la data puo' essere oltre le 3 proposte di default
            opts = sc.cerca_da(sess, id_)
        testo, opts = sc.avanza(sess, n, trova(opts, id_))
    return testo, opts


def prenota_reale(uid, ids, j=None):
    """Unico punto che usa mail/telefono reali: rifa' il percorso [sede, servizio, data, orario] e conferma."""
    p = profilo(uid)
    log.info('prenotazione con profilo uid=%s ids=%s', uid, ids)
    sess, opts = sc.login(p['email'], p['tel'])
    testo, _ = percorri(sess, opts, ids)
    if j and j['ora'] and not in_fascia(j, sc.ora_riepilogo(testo)):  # l'orario reale cambia a ogni richiesta
        sc.annulla(sess, testo)
        raise RuntimeError(f"orario {sc.ora_riepilogo(testo)} fuori dalla fascia {fascia(j)}")
    return sc.conferma(sess, testo)


def giro_monitoraggi():
    ora = time.time()
    controllati = False
    for j in list(UT['jobs']):
        if j['next'] > ora:
            continue
        controllati = True
        j['next'] = ora + POLL * random.uniform(0.8, 1.2)
        log.debug('controllo job %s', j['id'])
        try:
            if controlla(j):
                UT['jobs'].remove(j)
        except Exception as e:  # sito giu', slot preso da altri ecc.: si riprova al giro dopo
            log.error('job %s: %r', j['id'], e, exc_info=DEBUG)
    if controllati:  # scrivere solo se qualcosa e' cambiato: un salvataggio a vuoto sovrascrive modifiche al file
        salva()


# ---------- comandi ----------
def menu(uid, chat):
    say(chat, f'Il tuo id: {uid}' + ('' if autorizzato(uid) else '\nNon sei autorizzato: passa questo id all\'admin.')
               + '\n/prenota - prenota subito\n/monitora - prenota quando si libera\n/jobs - i tuoi monitoraggi\n/profilo - mail e telefono\n'
                 '/stop N - ferma il monitoraggio N\n/annulla - interrompi la procedura'
               + ('\n/autorizza ID, /revoca ID, /utenti (admin)' if uid in UT['admins'] else ''))


def elenco_jobs(uid, chat):
    """I monitoraggi (l'admin vede quelli di tutti), un bottone ciascuno: premendolo il monitoraggio viene fermato."""
    admin = uid in UT['admins']
    mine = [j for j in UT['jobs'] if admin or j['uid'] == uid]
    if not mine:
        return say(chat, 'Nessun monitoraggio attivo.')
    di = lambda j: f" [utente {j['uid']}]" if admin and j['uid'] != uid else ''  # l'admin distingue i monitoraggi altrui
    say(chat, ('Tutti i monitoraggi' if admin else 'I tuoi monitoraggi') + '. Premi su uno per fermarlo:\n\n'
        + '\n'.join(f"#{j['id']}{di(j)} {descr(j)}" for j in mine), None,
        [(f"Ferma #{j['id']}{di(j)}: {j['sede']} {j['data']}", f"s:{j['id']}") for j in mine])


def ferma_job(uid, chat, dati):
    jid = dati[2:]
    j = next((j for j in UT['jobs'] if str(j['id']) == jid and (j['uid'] == uid or uid in UT['admins'])), None)
    if not j:
        say(chat, 'Monitoraggio non trovato (gia\' fermato o concluso).')
    else:
        UT['jobs'].remove(j)
        salva()
        log.info('stop job %s da uid=%s', jid, uid)
        say(chat, f'Monitoraggio #{jid} fermato.')
    elenco_jobs(uid, chat)


def comando(uid, chat, t):
    cmd, _, arg = t.partition(' ')
    cmd = cmd.split('@')[0].lower()
    log.info('comando %s uid=%s', cmd, uid)
    if cmd in ('/start', '/help'):
        menu(uid, chat)
        if cmd == '/start' and not profilo(uid):  # primo accesso: subito il profilo, serve per prenotare
            modifica_profilo(uid, chat, 'Per iniziare imposta il tuo profilo (mail e telefono reali, usati solo per la conferma finale).\nLa tua email:')
        return
    if uid in UT['admins'] and cmd in ('/autorizza', '/revoca', '/utenti'):
        if cmd == '/utenti':
            return say(chat, 'Autorizzati: ' + (', '.join(UT['utenti']) or 'nessuno') + '\nAdmin: ' + ', '.join(map(str, UT['admins'])))
        if not arg.strip().isdigit():
            return say(chat, f'Uso: {cmd} ID')
        x = int(arg)
        if cmd == '/autorizza':
            UT['utenti'].setdefault(str(x), {})
        if cmd == '/revoca':
            UT['utenti'].pop(str(x), None)  # toglie autorizzazione e profilo
            UT['jobs'] = [j for j in UT['jobs'] if j['uid'] != x]
        salva()
        return say(chat, 'Fatto.')
    if not autorizzato(uid):
        return say(chat, f'Non autorizzato. Il tuo id e\' {uid}.')
    if cmd == '/profilo':
        p = profilo(uid)
        if not p:
            return modifica_profilo(uid, chat)
        return say(chat, f"Il tuo profilo:\nEmail: {p['email']}\nTelefono: {p['tel']}", None, [('Modifica', 'mod')])
    if cmd in ('/prenota', '/monitora'):
        return inizia(chat, uid, cmd[1:])
    if cmd == '/annulla':
        libera(uid)
        say(chat, 'Annullato.')
        return menu(uid, chat)
    if cmd == '/jobs' or (cmd == '/stop' and not arg.strip()):
        return elenco_jobs(uid, chat)
    if cmd == '/stop':
        n = len(UT['jobs'])
        UT['jobs'] = [j for j in UT['jobs'] if not (str(j['id']) == arg.strip() and (j['uid'] == uid or uid in UT['admins']))]
        salva()
        return say(chat, 'Fermato.' if len(UT['jobs']) < n else 'Monitoraggio non trovato.')
    say(chat, 'Comando sconosciuto, vedi /help.')


def gestisci(u):
    if 'callback_query' in u:
        q = u['callback_query']
        tg('answerCallbackQuery', callback_query_id=q['id'])
        log.debug('callback uid=%s dati=%s', q['from']['id'], q['data'])
        uid, chat, fn = q['from']['id'], q['message']['chat']['id'], lambda: callback(q['from']['id'], q['message']['chat']['id'], q['data'], q['message']['message_id'])
    elif 'message' in u and 'text' in u['message']:
        m = u['message']
        t = m['text'].strip()
        uid, chat = m['from']['id'], m['chat']['id']
        if m['chat']['type'] != 'private':
            return
        if not autorizzato(uid):
            chiedi_accesso(uid, m['from'])
        if t.startswith('/'):  # comando() gestisce da solo gli utenti non autorizzati
            fn = lambda: comando(uid, chat, t)
            return esegui(uid, chat, fn)
        fn = lambda: testo_utente(uid, chat, t)
    else:
        return
    esegui(uid, chat, fn)


RICHIESTE = {}  # uid -> quando ho avvisato gli admin l'ultima volta (max una richiesta all'ora per utente)


def chiedi_accesso(uid, frm):
    """Un utente non autorizzato ha scritto: avviso gli admin con i bottoni Autorizza / Rifiuta."""
    if time.time() - RICHIESTE.get(uid, 0) < 3600:
        return
    RICHIESTE[uid] = time.time()
    nome = ' '.join(filter(None, [frm.get('first_name'), frm.get('last_name')])) or '?'
    user = f" (@{frm['username']})" if frm.get('username') else ''
    log.info('richiesta di accesso uid=%s', uid)
    for admin in UT['admins']:
        say(admin, f'Richiesta di accesso al bot:\n{nome}{user}\nid: {uid}', None,
            [('Autorizza', f'a:{uid}'), ('Rifiuta', f'r:{uid}')])


def decidi_accesso(admin, chat, dati):
    if admin not in UT['admins']:
        return
    uid = int(dati[2:])
    if dati[0] == 'a':
        UT['utenti'].setdefault(str(uid), {})
        salva()
        log.info('uid=%s autorizzato da admin %s', uid, admin)
        say(chat, f'Utente {uid} autorizzato.')
        say(uid, 'Sei stato autorizzato! Scrivi /start per iniziare.')
    else:
        RICHIESTE[uid] = time.time()
        log.info('uid=%s rifiutato da admin %s', uid, admin)
        say(chat, f'Richiesta di {uid} rifiutata.')


def esegui(uid, chat, fn):
    if not autorizzato(uid):
        log.warning('accesso negato uid=%s', uid)
        return say(chat, f'Non sei autorizzato. Ho avvisato l\'admin: riceverai un messaggio quando sarai abilitato. Il tuo id e\' {uid}.')
    try:
        fn()
    except Exception as e:
        libera(uid)
        log.error('errore uid=%s: %r', uid, e, exc_info=DEBUG)
        say(chat, f'Errore: {e}\nProcedura interrotta, riparti con /prenota o /monitora.')


def main():
    offset = 0
    tg('setMyCommands', commands=[
        {'command': 'prenota', 'description': 'Prenota subito'},
        {'command': 'monitora', 'description': 'Prenota quando si libera'},
        {'command': 'jobs', 'description': 'I miei monitoraggi'},
        {'command': 'stop', 'description': 'Ferma un monitoraggio (/stop N)'},
        {'command': 'profilo', 'description': 'Mail e telefono'},
        {'command': 'annulla', 'description': 'Interrompi la procedura'},
        {'command': 'help', 'description': 'Elenco comandi'}])
    salva()  # il file esiste fin dall'avvio
    log.info('Bot avviato in modo %s. Admin: %s | utenti in %s | log level %s', MODO, UT['admins'], UTENTI_FILE, LOG_LEVEL)
    while True:
        try:
            ricarica()
            # attesa breve: su Windows Ctrl+C agisce solo a fine richiesta
            for u in tg('getUpdates', offset=offset, timeout=2) or []:
                offset = u['update_id'] + 1
                ricarica()
                gestisci(u)
            giro_monitoraggi()
        except requests.RequestException as e:
            log.warning('rete: %r', e)
            time.sleep(5)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        log.info('Bot fermato.')
