import { useEffect, useState } from "react";

type Source = { source: string; external_id: string; url: string | null; last_seen_at: string };
type Artist = { id: number; name: string };
type ArtistDetail = Artist & { aliases: string[]; concerts: Concert[] };
type Concert = {
  id: number;
  title: string;
  city: string;
  starts_at: string;
  venue: string | null;
  status: string;
  updated_at: string;
  sources: Source[];
  artists: Artist[];
};
type Profile = {
  id: number;
  display_name: string;
  city: string | null;
  notifications_enabled: boolean;
};
type Section = "concerts" | "artists" | "subscriptions" | "favorites" | "settings";

declare global {
  interface Window {
    Telegram?: { WebApp?: { initData: string; ready: () => void; expand: () => void } };
  }
}

const apiBase = import.meta.env.VITE_API_BASE_URL || "/api";
const initData = window.Telegram?.WebApp?.initData || "";

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`${apiBase}${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(initData ? { "X-Telegram-Init-Data": initData } : {}),
      ...options.headers,
    },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || `Ошибка ${response.status}`);
  }
  return response.json();
}

function dateLabel(value: string): string {
  return new Intl.DateTimeFormat("ru-RU", {
    day: "numeric", month: "long", hour: "2-digit", minute: "2-digit", timeZone: "Europe/Moscow",
  }).format(new Date(value));
}

function updatedLabel(value: string): string {
  return new Intl.DateTimeFormat("ru-RU", {
    day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", timeZone: "Europe/Moscow",
  }).format(new Date(value));
}

function sourceLabel(source: string): string {
  return ({ timepad: "Timepad", kudago: "KudaGo" } as Record<string, string>)[source] || source;
}

const sectionTitles: Record<Section, string> = {
  concerts: "Афиша", artists: "Артисты", subscriptions: "Подписки", favorites: "Избранное", settings: "Настройки",
};

export default function App() {
  const [section, setSection] = useState<Section>("concerts");
  const [city, setCity] = useState("Москва");
  const [date, setDate] = useState("");
  const [search, setSearch] = useState("");
  const [concerts, setConcerts] = useState<Concert[]>([]);
  const [artists, setArtists] = useState<Artist[]>([]);
  const [subscriptions, setSubscriptions] = useState<Artist[]>([]);
  const [favorites, setFavorites] = useState<Concert[]>([]);
  const [profile, setProfile] = useState<Profile | null>(null);
  const [selected, setSelected] = useState<Concert | null>(null);
  const [selectedArtist, setSelectedArtist] = useState<ArtistDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [page, setPage] = useState(0);
  const [total, setTotal] = useState(0);

  useEffect(() => {
    window.Telegram?.WebApp?.ready();
    window.Telegram?.WebApp?.expand();
    if (!initData) return;
    request<Profile>("/auth/telegram", { method: "POST", body: JSON.stringify({ init_data: initData }) })
      .then((signedIn) => { setProfile(signedIn); if (signedIn.city) setCity(signedIn.city); })
      .catch((cause) => setError(cause.message));
  }, []);

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError("");
    const query = new URLSearchParams({ city, limit: "20", offset: String(page * 20) });
    if (date) query.set("from", date);
    if (search.trim()) query.set("q", search.trim());
    request<{ items: Concert[]; total: number }>(`/concerts?${query}`)
      .then((catalog) => { if (active) { setConcerts(catalog.items); setTotal(catalog.total); } })
      .catch((cause) => { if (active) setError(cause.message); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [city, date, search, page]);

  useEffect(() => {
    if (section !== "artists") return;
    setLoading(true);
    request<{ items: Artist[] }>(`/artists?q=${encodeURIComponent(search)}`)
      .then((catalog) => setArtists(catalog.items))
      .catch((cause) => setError(cause.message))
      .finally(() => setLoading(false));
  }, [section, search]);

  useEffect(() => {
    if (!profile) return;
    if (section === "subscriptions") {
      request<Artist[]>("/me/subscriptions").then(setSubscriptions).catch((cause) => setError(cause.message));
    }
    if (section === "favorites") {
      request<Concert[]>("/me/favorites").then(setFavorites).catch((cause) => setError(cause.message));
    }
  }, [profile, section]);

  async function toggleFavorite(concert: Concert) {
    if (!profile) { setError("Избранное доступно после входа через Telegram."); return; }
    const saved = favorites.some((favorite) => favorite.id === concert.id);
    try {
      await request(`/me/favorites/${concert.id}`, { method: saved ? "DELETE" : "PUT" });
      setFavorites(saved ? favorites.filter((favorite) => favorite.id !== concert.id) : [...favorites, concert]);
    } catch (cause) { setError((cause as Error).message); }
  }

  async function toggleSubscription(artist: Artist) {
    if (!profile) { setError("Подписки доступны после входа через Telegram."); return; }
    const subscribed = subscriptions.some((entry) => entry.id === artist.id);
    try {
      await request(`/me/subscriptions/${artist.id}`, { method: subscribed ? "DELETE" : "PUT" });
      setSubscriptions(subscribed ? subscriptions.filter((entry) => entry.id !== artist.id) : [...subscriptions, artist]);
    } catch (cause) { setError((cause as Error).message); }
  }

  async function saveSettings(changes: Partial<Profile>) {
    if (!profile) return;
    try {
      const updated = await request<Profile>("/me", { method: "PATCH", body: JSON.stringify(changes) });
      setProfile(updated);
      if (updated.city) setCity(updated.city);
    } catch (cause) { setError((cause as Error).message); }
  }

  function showConcert(concert: Concert) {
    setSelected(concert);
    setSelectedArtist(null);
    setError("");
  }

  async function showArtist(artist: Artist) {
    setLoading(true);
    setError("");
    try {
      setSelectedArtist(await request<ArtistDetail>(`/artists/${artist.id}`));
      setSelected(null);
    } catch (cause) { setError((cause as Error).message); }
    finally { setLoading(false); }
  }

  function concertCard(concert: Concert) {
    return <article className="concert-card" key={concert.id}>
      <button className="card-main" onClick={() => showConcert(concert)} aria-label={`Открыть ${concert.title}`}>
        <span className="card-date">{dateLabel(concert.starts_at)}</span>
        <strong>{concert.title}</strong>
        <span className="card-meta">{concert.venue || concert.city}</span>
        {concert.artists.length > 0 && <span className="artist-line">{concert.artists.map((artist) => artist.name).join(" · ")}</span>}
      </button>
      <button className={`save-button ${favorites.some((entry) => entry.id === concert.id) ? "saved" : ""}`} onClick={() => toggleFavorite(concert)} aria-label="Добавить в избранное">☆</button>
    </article>;
  }

  return <div className="app-shell">
    <header className="topbar">
      <button className="brand" onClick={() => { setSection("concerts"); setSelected(null); setSelectedArtist(null); }}><span className="brand-icon">✺</span> GigRadar</button>
      <span className="topbar-caption">Твоя музыка. Твои концерты.</span>
    </header>

    <main>
      {error && <div className="error-banner" role="alert">{error}<button onClick={() => setError("")} aria-label="Закрыть">×</button></div>}
      {selected ? <section className="detail-page">
        <button className="back-button" onClick={() => setSelected(null)}>← Назад</button>
        <div className="eyebrow">{selected.city} · {dateLabel(selected.starts_at)}</div>
        <h1>{selected.title}</h1>
        <p className="detail-venue">{selected.venue || "Площадка уточняется"}</p>
        {selected.status === "postponed" && <div className="notice">Организатор изменил время события. Проверьте информацию у источника.</div>}
        {selected.status === "cancelled" && <div className="notice">Источник подтвердил отмену события.</div>}
        <div className="detail-actions"><button className="primary-button" onClick={() => toggleFavorite(selected)}>{favorites.some((entry) => entry.id === selected.id) ? "В избранном" : "☆ В избранное"}</button></div>
        {selected.artists.length > 0 && <div className="detail-block"><h2>Исполнители</h2>{selected.artists.map((artist) => <button className="artist-pill" key={artist.id} onClick={() => showArtist(artist)}>{artist.name} →</button>)}</div>}
        <div className="detail-block"><h2>Источники</h2>{selected.sources.map((source) => <p key={`${source.source}:${source.external_id}`}>{source.url?.startsWith("https://") ? <a href={source.url} target="_blank" rel="noopener noreferrer">{sourceLabel(source.source)} ↗</a> : <span>{sourceLabel(source.source)}</span>}<small>Обновлено {updatedLabel(source.last_seen_at)}</small></p>)}</div>
      </section> : selectedArtist ? <section className="detail-page">
        <button className="back-button" onClick={() => setSelectedArtist(null)}>← Назад</button>
        <div className="eyebrow">Исполнитель</div>
        <h1>{selectedArtist.name}</h1>
        {selectedArtist.aliases.length > 0 && <p className="detail-venue">Также: {selectedArtist.aliases.join(", ")}</p>}
        <div className="detail-actions"><button className="primary-button" onClick={() => toggleSubscription(selectedArtist)}>{subscriptions.some((entry) => entry.id === selectedArtist.id) ? "Вы подписаны" : "+ Подписаться"}</button></div>
        <div className="detail-block"><h2>Ближайшие концерты</h2>{selectedArtist.concerts.length ? <div className="concert-list">{selectedArtist.concerts.map(concertCard)}</div> : <p className="state">Ближайших концертов пока нет.</p>}</div>
      </section> : <>
        <div className="page-heading"><div className="eyebrow">Музыка рядом</div><h1>{sectionTitles[section]}</h1></div>

        {section === "concerts" && <section>
          <div className="filters"><label>Город<select value={city} onChange={(event) => { setCity(event.target.value); setPage(0); }}><option>Москва</option><option>Санкт-Петербург</option></select></label><label>Дата с<input type="date" value={date} onChange={(event) => { setDate(event.target.value); setPage(0); }} /></label></div>
          <label className="search-label"><span>Поиск</span><input placeholder="Концерт или артист" value={search} onChange={(event) => { setSearch(event.target.value); setPage(0); }} /></label>
          {loading ? <p className="state">Загружаем афишу…</p> : concerts.length ? <><div className="concert-list">{concerts.map(concertCard)}</div><div className="pagination"><button disabled={page === 0} onClick={() => setPage(page - 1)}>← Ранее</button><span>{page * 20 + 1}–{Math.min((page + 1) * 20, total)} из {total}</span><button disabled={(page + 1) * 20 >= total} onClick={() => setPage(page + 1)}>Далее →</button></div></> : <p className="state">По этим условиям концертов пока нет. Попробуйте другой город или дату.</p>}
        </section>}

        {section === "artists" && <section><label className="search-label"><span>Поиск артиста</span><input placeholder="Имя исполнителя" value={search} onChange={(event) => setSearch(event.target.value)} /></label>{loading ? <p className="state">Ищем артистов…</p> : artists.length ? <div className="artist-list">{artists.map((artist) => <div className="artist-row" key={artist.id}><button className="artist-name" onClick={() => showArtist(artist)}>{artist.name}</button><button onClick={() => toggleSubscription(artist)}>{subscriptions.some((entry) => entry.id === artist.id) ? "Подписаны ✓" : "+ Подписаться"}</button></div>)}</div> : <p className="state">Артистов пока нет.</p>}</section>}

        {section === "subscriptions" && <section>{!profile ? <p className="state">Откройте приложение в Telegram, чтобы подписываться на артистов.</p> : subscriptions.length ? <div className="artist-list">{subscriptions.map((artist) => <div className="artist-row" key={artist.id}><button className="artist-name" onClick={() => showArtist(artist)}>{artist.name}</button><button onClick={() => toggleSubscription(artist)}>Отписаться</button></div>)}</div> : <p className="state">Вы пока не подписаны на артистов. Найдите их в каталоге.</p>}</section>}

        {section === "favorites" && <section>{!profile ? <p className="state">Откройте приложение в Telegram, чтобы сохранять концерты.</p> : favorites.length ? <div className="concert-list">{favorites.map(concertCard)}</div> : <p className="state">Здесь появятся концерты, которые вы сохранили.</p>}</section>}

        {section === "settings" && <section className="settings-panel">{!profile ? <p className="state">Настройки доступны после входа через Telegram.</p> : <><p className="profile-name">{profile.display_name}</p><label>Мой город<select value={profile.city || city} onChange={(event) => saveSettings({ city: event.target.value })}><option>Москва</option><option>Санкт-Петербург</option></select></label><label className="switch-row"><span>Уведомления о концертах</span><input type="checkbox" checked={profile.notifications_enabled} onChange={(event) => saveSettings({ notifications_enabled: event.target.checked })} /></label></>}</section>}
      </>}
    </main>

    <nav className="bottom-nav" aria-label="Разделы">
      {(["concerts", "artists", "subscriptions", "favorites", "settings"] as Section[]).map((entry) => <button key={entry} className={section === entry && !selected && !selectedArtist ? "active" : ""} onClick={() => { setSelected(null); setSelectedArtist(null); setSection(entry); setError(""); }}><span>{({ concerts: "⌁", artists: "♫", subscriptions: "◎", favorites: "☆", settings: "⚙" } as Record<Section, string>)[entry]}</span>{sectionTitles[entry]}</button>)}
    </nav>
  </div>;
}
