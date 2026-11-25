# Peron — automatyczne dane kolejowe

Codzienny importer dla [Peronu](https://peron-kolej-live.fabledbench32.chatgpt.site).

- **Linie**: [PolishTrainsGTFS](https://mkuran.pl/gtfs/), publiczny rozkład GTFS. Dopasowanie po numerze pociągu, przewoźniku i dacie kursowania.
- **Kierunki w Halinowie**: z kolejności przystanków w `stop_times.txt` i długości geograficznej następnej stacji po Halinowie. Brak ręcznej listy stacji końcowych. Niepewne przypadki pozostają nieustalone.
- **Planowany tabor KM**: [oficjalne zestawienia](https://www.mazowieckie.com.pl/pl/rozklad-jazdy). Odczyt numeru pociągu, zestawienia, liczby jednostek i dni obowiązywania.
- **Odjazdy/opóźnienia na żywo**: pobierane oddzielnie przez Peron z Bilkom / KalkulatorKolejowy.pl.

GitHub Actions uruchamia `scripts/update_data.py` codziennie o 03:17 UTC oraz po zmianach importera. Wynik: `data/peron.json` (bez danych prywatnych).

Nieznane lub konfliktujące przypisania są pomijane. Ten plik podaje tabor **planowany**, a nie gwarantowany. Jeśli źródło jest niedostępne lub nowa publikacja nie zostanie poprawnie rozpoznana, aktualizacja kończy się błędem i zachowuje ostatni poprawny plik; Peron powinien pokazywać znacznik wieku danych.

Do uruchomienia lokalnie: `pip install -r requirements.txt`, następnie `python scripts/update_data.py`.
