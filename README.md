# ghost_telephonist
not yet tested only gpt thoughts

Voici comment utiliser **ce diff** pour “Ghost Telephonist”, et à quoi servent précisément les ajouts/modifs.

# Ce que change le patch (utile pour Ghost Telephonist)

* **Boucle d’appels pilotée par timers (auto-call / auto-hangup)**
  Ajout de timers côté VTY (`tick_timer_call`, `tick_timer_hangup`) + compteur `calls`. La commande `call` déclenche un appel, puis un **raccrochage programmé \~3,7 s plus tard**, et relance un nouvel appel dès retour en “normal service”. Ça crée des **bursts d’appels contrôlés dans le temps**, parfaits pour caler vos essais sur les fenêtres de paging / CSFB. ([GitHub][1])
* **Exposition d’un socket UNIX dédié “TMSI”**
  `osmocon` accepte `-t /tmp/osmocom_mi` et enregistre un **tool server** sur un DLCI TMSI. Idéal pour qu’un contrôleur externe lise les **MI/TMSI/événements** en temps réel et déclenche la logique Ghost Telephonist au bon instant. ([GitHub][1])
* **Horodatage milliseconde sur la console HDLC**
  Impression de timestamps `YYYY-mm-dd HH:MM:SS.mmm` pour **corréler finement** avec vos captures LTE/GSM (RRC Paging, PCH, AGCH) ou traces eNodeB/BSC. ([GitHub][1])
* **Logs radio enrichis**
  `cell_log.c` log maintenant **LAC** en plus d’ARFCN/MCC/MNC et écrit aussi en fichier (LOGFILE). Utile pour vérifier la **zone LAC** pendant la transition CSFB / reselection 2G. ([GitHub][1])
* **Accès public à `gsm48_mm_release_mm_conn()`**
  Rendu non-static pour permettre un **raccrochage forcé** depuis la VTY (utile au “hijack windowing”). ([GitHub][1])
* **TX activé côté firmware**
  `CONFIG_TX_ENABLE` est activé : la **C123** peut réellement **émettre** (nécessaire pour placer/raccrocher à la volée). ([GitHub][1])
* **Divers perf/robustesse**
  `-O3` pour la build host et petite correction d’allocation dans le décodage 7-bit (évite un `calloc/free`). ([GitHub][1])

# Comment l’exploiter dans un setup “Ghost Telephonist”

1. **Patcher & builder Osmocom-BB**
   Applique le diff sur ton arbre osmocom-bb, rebuild **firmware** + **host**. Vérifie que la cible firmware a bien le TX activé. ([GitHub][1])
2. **Démarrer `osmocon` avec le socket TMSI**
   Exemple : `osmocon -p /dev/ttyUSBX -s /tmp/osmocom_l2 -t /tmp/osmocom_mi …`
   Garde le chemin du socket `/tmp/osmocom_mi` pour ton process “orchestrateur” (ghost-controller). ([GitHub][1])
3. **Lancer `mobile` (layer23) et créer l’MS**
   Depuis la VTY, sélectionne l’MS (`show ms`, `enable`, etc.). Les logs incluront LAC/ARFCN; la console HDLC est horodatée. ([GitHub][1])
4. **Armer la boucle d’appels synchronisés**

   * Démarre : `call 1 <NUMERO>` → **calls=1** ; hangup auto à \~3.7 s ; **relance auto** dès `normal service`.
   * Stop net : `call 1 kill` (met `calls=0` et raccroche).
     Cette boucle **martèle** des SETUP/RR en créant des fenêtres très régulières pour **coïncider** avec tes événements de **paging / CSFB** observés côté LTE. ([GitHub][1])
5. **Brancher le contrôleur Ghost Telephonist**
   Ton process externe se connecte à `/tmp/osmocom_mi` pour lire les **MI/TMSI/états** et :

   * **Détecte** le paging / fallback (corrélation via timestamps HDLC + sniffer LTE/GSM).
   * **Décide** quand forcer un **hangup** immédiat (raccourcir la fenêtre) ou **relancer** un appel pour “coller” au paging du terminal cible.
   * **Journalise** LAC/ARFCN pour s’assurer d’être sur la même zone que le mobile victime pendant la transition. ([GitHub][1])

# Pourquoi c’est utile pour Ghost Telephonist

* Le cœur de Ghost Telephonist est un **jeu de timing** autour du **paging et de la transition CSFB**. Le patch te donne :

  * un **métronome d’appels** ultra-prévisible (timers) pour “balayer” le bon créneau,
  * une **télémetrie** exploitable (TMSI socket + timestamps + LAC),
  * la capacité de **raccrocher/relancer** sans latence (fonction libérée + TX actif),
  * des **logs corrélables** aux traces RRC/PCH pour vérifier que tu tapes au bon moment. ([GitHub][1])

# Séquence type (terrain de test / PLMN privé)

1. Sniffer LTE RRC Paging + GSM PCH (et garde les horodatages).
2. Utilise un UE “leurre” pour provoquer un **CSFB** vers 2G (appel entrant sur la cible).
3. La C123 patchée boucle des **MO SETUP / HANGUP** en cadence.
4. Dès que ton orchestrateur voit le **paging cible** (ou un indice TMSI associé), il ajuste :

   * soit **raccroche** pour libérer/prendre la ressource au bon instant,
   * soit **relance** pour **s’aligner** sur l’assignation radio 2G.
5. Observe si tu **gagnes la course** (assignment/sdcch) au moment critique de la bascule CSFB. Les logs HDLC+LAC t’aident à diagnostiquer. ([GitHub][1])

# Commandes clés ajoutées/altérées

* `osmocon … -t /tmp/osmocom_mi` → **serveur TMSI**.
* `call 1 <num>` → démarre la **boucle d’appels** (auto-hangup + auto-retry).
* `call 1 kill` → **arrêt propre** de la boucle.
* Hangup forcé via `gsm48_mm_release_mm_conn()` exposée (utilisée par `call_hangup`). ([GitHub][1])

> ⚠️ Légal & sécu : ne teste que sur **PLMN privé / SIMs de test** avec accords explicites. La manip implique émission 2G et interactions cœur réseau : hors cadre autorisé, c’est illégal.

Si tu veux, je te file un mini “ghost-controller.py” qui lit `/tmp/osmocom_mi`, consomme les timestamps HDLC et pilote la VTY (`telnet localhost 4247`) pour caler les hangup/re-call au ms près.

[1]: https://raw.githubusercontent.com/bbaranoff/plmn-research/refs/heads/master/projects/pagfun/fun_with_paging_4f0acac4c1fa538082f54cb14bef0841aa9c8abb.diff "raw.githubusercontent.com"
