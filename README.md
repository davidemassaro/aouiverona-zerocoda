# Bot Telegram per prenotazioni ULSS 9 Scaligera

Bot Telegram che prenota un appuntamento sul portale `ulss9scaligera.prenotami.cloud`
(punto prelievi) oppure monitora una fascia di date e orari e prenota quando si libera uno slot.

## Come usarlo

- **Self hosted**: puoi far girare il bot sul tuo computer o su un tuo server, con il tuo token e i tuoi
  utenti (vedi [Installazione](#installazione)). I tuoi dati restano sul tuo computer.
- **Bot già pronto**: ne esiste già uno online, [@ScaligeraPrenotami_bot](https://t.me/ScaligeraPrenotami_bot).
  Non garantiamo che sia sempre attivo o funzionante. Per usarlo devi essere autorizzato dall'admin:
  scrivigli e l'admin riceve la tua richiesta.

## File

| File | Cosa fa |
|---|---|
| `bot.py` | Il bot Telegram |
| `check-scaligera.py` | Libreria per parlare col portale; usabile anche da riga di comando |
| `.env` | Configurazione e token (non committato, copia di `.env.example`) |
| `utenti.json` | Admin, utenti autorizzati con il loro profilo e i monitoraggi attivi (non committato) |

## Installazione

Serve Python 3 e la libreria `requests`:

```bash
py -m pip install requests
```

1. Crea il bot su Telegram con @BotFather (`/newbot`) e copia il token.
2. Copia `.env.example` in `.env` e compila almeno `TELEGRAM_TOKEN` e `ADMIN_ID`
   (il tuo id Telegram, lo vedi scrivendo a @userinfobot).
3. Avvia il bot:

```bash
py bot.py
```

All'avvio il bot crea `utenti.json` con te come admin e registra i comandi nel menu di Telegram.
`Ctrl+C` lo ferma in un paio di secondi.

## Configurazione (`.env`)

| Variabile | Significato | Default |
|---|---|---|
| `TELEGRAM_TOKEN` | Token di BotFather | obbligatoria |
| `ADMIN_ID` | Id Telegram dell'admin. Serve solo per creare `utenti.json` la prima volta; dopo gli admin si gestiscono nel file | - |
| `BOT_MODE` | `private`: ogni nuovo utente deve essere approvato da un admin. `public`: il bot accetta tutti | `private` |
| `LOG_LEVEL` | `DEBUG`, `INFO`, `WARNING` o `ERROR` | `INFO` |
| `POLL_SECONDS` | Ogni quanti secondi controllare ogni monitoraggio | `120` |
| `FAKE_EMAIL`, `FAKE_PHONE` | Mail e telefono fittizi usati per consultare il portale | valori di esempio |

## Uso

Comandi (sono anche nel menu di Telegram):

| Comando | Cosa fa |
|---|---|
| `/prenota` | Prenota subito: scegli sede, servizio, data e orario con i bottoni |
| `/monitora` | Monitora una finestra di date e una fascia oraria |
| `/jobs` | Elenca i tuoi monitoraggi; premi un bottone per fermarne uno |
| `/stop N` | Ferma il monitoraggio N (senza numero mostra l'elenco con i bottoni) |
| `/profilo` | Mostra mail e telefono salvati; "Modifica" per cambiarli |
| `/annulla` | Interrompe la procedura in corso |
| `/help` | Elenco dei comandi |

In ogni passo ci sono **« Indietro** e **Annulla**.

### Profilo

La prima volta il bot chiede mail e telefono reali e li salva nel tuo profilo. Si usano **solo
al momento di prenotare**: per navigare e per monitorare il portale il bot usa sempre
`FAKE_EMAIL` e `FAKE_PHONE`, così non rischi blocchi sui tuoi dati.

### Prenotazione (`/prenota`)

1. Sede, servizio (se c'è un solo servizio viene scelto in automatico), data e orario.
2. Il bot mostra il riepilogo, con l'orario vero che assegna il portale, e chiede conferma.
3. Premendo **Conferma prenotazione** prenota con il tuo profilo. Se nel frattempo lo slot è stato
   preso ricevi "Orario non più disponibile".

Mentre decidi **non resta nessun posto bloccato** sul portale: il riepilogo viene letto e rilasciato subito.

### Monitoraggio (`/monitora`)

1. Sede e servizio.
2. Inizio e fine della finestra di date (calendario a bottoni, o scrivi `GG/MM/AAAA`).
   Anche date molto lontane. Fine: "Solo quel giorno", una data o "Nessun limite".
3. Fascia oraria "dalle - alle" (es. `08:00` - `08:05`) oppure `tutti`. Conta l'**orario vero**
   dell'appuntamento, non l'inizio dello slot di mezz'ora.
4. Come procedere quando c'è uno slot valido:
   - **Prenota in automatico**: il bot prenota da solo (e controlla di nuovo la fascia prima di confermare).
   - **Avvisami e confermo io**: il bot ti manda un messaggio con gli slot trovati; premi uno slot,
     vedi il riepilogo e confermi. Gli slot già notificati non vengono rimandati.

Quando uno slot viene prenotato il monitoraggio si chiude.

### Utenti e admin

- **Modo privato** (`BOT_MODE=private`, predefinito): puo' usare il bot solo chi e' autorizzato.
- **Modo pubblico** (`BOT_MODE=public`): chiunque scriva al bot puo' usarlo, senza approvazione;
  ognuno inserisce il proprio profilo e vede solo i propri monitoraggi. In questo modo non c'e' modo
  di bloccare un singolo utente, e la tabella `utenti` in `utenti.json` contiene chi ha salvato un profilo.
  Conviene solo se hai fiducia nel pubblico: ogni monitoraggio fa richieste al portale.
- Nel modo privato, chi non è autorizzato riceve un avviso e **gli admin ricevono una richiesta** con i bottoni
  **Autorizza** / **Rifiuta** (al massimo una richiesta all'ora per utente).
- Comandi solo admin: `/autorizza ID`, `/revoca ID` (toglie anche profilo e monitoraggi), `/utenti`.
- L'admin vede e può fermare i monitoraggi di tutti, con l'etichetta `[utente ID]`.

## `utenti.json`

```json
{
 "admins": [123456789],
 "utenti": { "123456789": { "email": "nome@example.com", "tel": "3331234567" } },
 "jobs": [],
 "next_id": 1
}
```

Il file si può modificare a mano mentre il bot gira: viene riletto da solo quando cambia
(se il JSON è sbagliato il bot lo segnala nel log e tiene lo stato in memoria).
Contiene dati personali: non committarlo (è nel `.gitignore`).

## Log

Il livello si imposta con `LOG_LEVEL`:

- `INFO`: avvio, comandi, monitoraggi creati/fermati, avvisi, prenotazioni, richieste di accesso.
- `WARNING` / `ERROR`: problemi di rete, errori dei monitoraggi, errori di Telegram.
- `DEBUG`: anche ogni richiesta al portale e, per ogni controllo, date, orari, orari reali e slot scartati con il motivo.

Nei log non compaiono token, mail e telefono.

## Riga di comando (senza bot)

```bash
py check-scaligera.py -e MAIL -t TELEFONO                       # sedi
py check-scaligera.py -e MAIL -t TELEFONO -s MARZANA            # + servizi
py check-scaligera.py -e MAIL -t TELEFONO -s MARZANA -v accesso # + date
py check-scaligera.py -e MAIL -t TELEFONO -s MARZANA -v accesso -d 20261106            # + orari
py check-scaligera.py -e MAIL -t TELEFONO -s MARZANA -v accesso --da 20261201          # date libere da quel giorno
py check-scaligera.py ... -o CODA_01_ven_0900_0930              # riepilogo (poi annulla)
py check-scaligera.py ... -o CODA_01_ven_0900_0930 --conferma   # prenota davvero
```

## Note

- Il portale assegna l'orario esatto a ogni richiesta (es. slot 08:00-08:30 → 08:12) e può cambiarlo
  tra l'avviso e la conferma.
- Ogni slot candidato viene aperto un attimo per leggerne l'orario e rilasciato subito;
  con finestre molto larghe un controllo fa più richieste.
- Il bot è una procedura non ufficiale che usa le pagine pubbliche del portale: se il sito
  cambia struttura può smettere di funzionare.
