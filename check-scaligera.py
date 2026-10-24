"""Disponibilita' ULSS 9 Scaligera (prenotami.cloud): sedi -> servizi -> date -> orari.
Libreria usata anche da bot.py; da riga di comando:

  py check-scaligera.py -e MAIL -t TELEFONO                      # elenca le sedi
  py check-scaligera.py -e MAIL -t TELEFONO -s MARZANA           # + servizi
  py check-scaligera.py -e MAIL -t TELEFONO -s MARZANA -v TIPO_07            # + date
  py check-scaligera.py -e MAIL -t TELEFONO -s MARZANA -v TIPO_07 -d 20261106  # + orari
  py check-scaligera.py -e MAIL -t TELEFONO -s MARZANA -v TIPO_07 -d 20261106 -o CODA_01_ven_0900_0930   # riepilogo (poi annulla)
  ... stesso comando + --conferma                                # PRENOTA davvero
  py check-scaligera.py -e MAIL -t TELEFONO --tutto              # percorre tutto l'albero

-s/-v/-d/-o accettano l'id (MARZANA) o una parte dell'etichetta ("marzana", "accesso").
Senza --conferma e' solo lettura: il posto scelto con -o viene rilasciato (annulla).
"""
import argparse
import html
import logging
import re
import sys
from datetime import datetime

import requests

log = logging.getLogger('scaligera')
BASE = 'https://ulss9scaligera.prenotami.cloud/mobile/'
# (pagina a cui si fa POST, campo hidden che identifica la scelta, nome livello)
LIVELLI = [
    ('gest_sedi.html', 'id_istanza_p', 'sede'),
    ('gest_servizi.html', 'id_tipo_serv_p', 'servizio'),
    ('gest_date.html', 'data_p', 'data'),
    ('gest_orari.html', 'id_orario_p', 'orario'),
]

FORM = re.compile(r'<form[^>]*>(.*?)</form>', re.S)
SUBMIT = re.compile(r'<input type="submit"[^>]*name="([^"]+)"[^>]*value="([^"]*)"')
HIDDEN = re.compile(r'<input type="hidden"[^>]*name="([^"]+)" value="([^"]*)"')


def opzioni(testo, campo):
    """Estrae [(id, etichetta, nome_submit)] dai form che hanno il campo hidden `campo`."""
    out = []
    for corpo in FORM.findall(testo):
        h = dict(HIDDEN.findall(corpo))
        s = SUBMIT.search(corpo)
        if campo in h and s:
            out.append((h[campo], html.unescape(s.group(2)), s.group(1)))
    return out


def scegli(opts, filtro, livello):
    f = filtro.lower()
    for o in opts:
        if f == o[0].lower():
            return o
    trovate = [o for o in opts if f in o[1].lower() or f in o[0].lower()]
    if len(trovate) != 1:
        raise ValueError(f'{livello} "{filtro}": {len(trovate)} corrispondenze, disponibili: '
                         + ', '.join(f'{o[0]} ({o[1]})' for o in opts))
    return trovate[0]


def stampa(livello, opts, indent=0):
    print(f'{" " * indent}{livello}:' if opts else f'{" " * indent}{livello}: nessuna disponibilita\'')
    for i, label, _ in opts:
        print(f'{" " * indent}  {i:<28} {label}')


def post(sess, pagina, dati):
    log.debug('POST %s', pagina)
    r = sess.post(BASE + pagina, data=dati, timeout=30)
    r.raise_for_status()
    return r.text


def login(email, telefono, prefisso='39'):
    """Apre una sessione. Ritorna (sess, opzioni_sedi)."""
    sess = requests.Session()
    sess.headers['User-Agent'] = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/133.0.0.0 Safari/537.36'
    sess.headers['Referer'] = BASE + 'inizio.html'
    testo = post(sess, 'gest_inizio.html', {
        'name_email': email,
        'name_telefono': telefono,
        'country_code_name_telefono': prefisso,
        'name_avanti_con_mail': 'avanti',
    })
    opts = opzioni(testo, LIVELLI[0][1])
    if not opts:
        log.error('login: nessuna sede nella risposta')
        raise RuntimeError('Nessuna sede trovata: controlla mail/telefono o se il sito e\' cambiato.')
    return sess, opts


def avanza(sess, n, scelta):
    """Sceglie `scelta` al livello n. Ritorna (testo, opzioni del livello n+1; None dopo l'orario)."""
    i, label, sub = scelta
    testo = post(sess, LIVELLI[n][0], {sub: label, LIVELLI[n][1]: i})
    return testo, (opzioni(testo, LIVELLI[n + 1][1]) if n < 3 else None)


GIORNI = ['lunedi', 'martedi', 'mercoledi', 'giovedi', 'venerdi', 'sabato', 'domenica']
MESI = ['gennaio', 'febbraio', 'marzo', 'aprile', 'maggio', 'giugno', 'luglio', 'agosto', 'settembre',
        'ottobre', 'novembre', 'dicembre']


def cerca_da(sess, data):
    """Dal livello servizio: le prime 3 date disponibili a partire da `data` (AAAAMMGG), anche lontane."""
    d = datetime.strptime(data, '%Y%m%d')
    testo = post(sess, 'gest_servizi.html', {
        'name_calendario': f'{GIORNI[d.weekday()]} {d.day} {MESI[d.month - 1]} {d.year}',
        'name_calendario_submit': d.strftime('%d-%m-%Y'),
        'name_cerca_disponibilita': 'cerca disponibilità'.encode('cp1252'),
    })
    return opzioni(testo, LIVELLI[2][1])


def ora_riepilogo(testo):
    """Orario reale assegnato dal sito nella pagina riepilogo (es. '07:35'), diverso dall'inizio dello slot."""
    m = re.search(r'alle ore\s*(\d{1,2}:\d{2})', testo)
    return m.group(1).zfill(5) if m else None


def riepilogo(testo):
    m = re.search(r'Stai prenotando[^<]*', testo)
    return html.unescape(m.group(0)) if m else 'Riepilogo non trovato'


def annulla(sess, testo):
    m = re.search(r'name="annulla_p" value="([^"]*)"', testo)
    if m:
        post(sess, 'annulla.html', {'name_annulla': 'annulla', 'annulla_p': m.group(1)})


def conferma(sess, testo, note=''):
    """Conferma la prenotazione dalla pagina riepilogo. Ritorna il testo del biglietto."""
    form = re.search(r'<form[^>]*action="(conferma\.html[^"]*)"', testo)
    if not form:
        raise RuntimeError('Form di conferma non trovato: lo slot e\' stato preso o il sito e\' cambiato.')
    desc = re.search(r'name="name_descrizione_note_cliente" value="([^"]*)"', testo)
    testo = post(sess, form.group(1), {
        'name_note_cliente': note,
        'name_descrizione_note_cliente': html.unescape(desc.group(1)) if desc else '',
        'name_conferma': 'conferma',
    })
    esito = re.search(r'Hai prenotato.*?(?=<hr)', testo, re.S)
    if not esito:
        raise RuntimeError('Conferma non riuscita (nessun biglietto nella risposta).')
    return re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', ' ', esito.group(0)))).strip()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('-e', '--email', required=True)
    ap.add_argument('-t', '--telefono', required=True, help='senza prefisso, es. 3331234567')
    ap.add_argument('-p', '--prefisso', default='39')
    ap.add_argument('-s', '--sede')
    ap.add_argument('-v', '--servizio')
    ap.add_argument('-d', '--data', help='AAAAMMGG o parte dell\'etichetta')
    ap.add_argument('--da', help='AAAAMMGG: mostra le prime 3 date libere da quel giorno in poi')
    ap.add_argument('-o', '--orario', help='id o parte dell etichetta')
    ap.add_argument('-n', '--note', default='', help='note per la prenotazione')
    ap.add_argument('--conferma', action='store_true', help='conferma davvero la prenotazione')
    ap.add_argument('--tutto', action='store_true', help='elenca ricorsivamente tutti i livelli')
    a = ap.parse_args()

    sess, opts = login(a.email, a.telefono, a.prefisso)

    if a.tutto:
        def scendi(opts, n, indent):
            stampa(LIVELLI[n][2], opts, indent)
            if n < 3:
                for o in opts:
                    print(f'{" " * indent}> {o[1]}')
                    scendi(avanza(sess, n, o)[1], n + 1, indent + 2)
        scendi(opts, 0, 0)
        return

    testo = None
    for n, filtro in enumerate([a.sede, a.servizio, a.data, a.orario]):
        if n == 2 and (a.da or re.fullmatch(r'\d{8}', filtro or '') and filtro not in [o[0] for o in opts]):
            opts = cerca_da(sess, a.da or filtro)  # data oltre le 3 proposte di default
        stampa(LIVELLI[n][2], opts)
        if not filtro:
            return
        scelta = scegli(opts, filtro, LIVELLI[n][2])
        print(f'\n> {LIVELLI[n][2]}: {scelta[1]}\n')
        testo, opts = avanza(sess, n, scelta)

    print(riepilogo(testo))
    if not a.conferma:
        annulla(sess, testo)
        print('\nNon confermato (posto rilasciato). Aggiungi --conferma per prenotare.')
        return
    print('\nPRENOTATO:', conferma(sess, testo, a.note))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError) as e:
        sys.exit(str(e))
