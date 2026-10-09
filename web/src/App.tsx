import { useEffect, useState } from "react";
import { text } from "./text";

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
type WebSession = { profile: Profile; csrf_token: string };
type WebLoginConfig = { enabled: boolean; login_url: string | null };
type Section = keyof typeof text.sections;

declare global {
  interface Window {
    Telegram?: { WebApp?: { initData: string; ready: () => void; expand: () => void } };
  }
}

const apiBase = import.meta.env.VITE_API_BASE_URL || "/api";
const initData = window.Telegram?.WebApp?.initData || "";

class ApiError extends Error {
  constructor(message: string, readonly status: number) { super(message); }
}

async function request<T>(path: string, options: RequestInit = {}, csrf = ""): Promise<T> {
  const response = await fetch(`${apiBase}${path}`, {
    ...options,
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      ...(initData ? { "X-Telegram-Init-Data": initData } : {}),
      ...(csrf ? { "X-CSRF-Token": csrf } : {}),
      ...options.headers,
    },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new ApiError(typeof body.detail === "string" ? body.detail : text.errors.http(response.status), response.status);
  }
  return response.json();
}

function dateLabel(value: string): string {
  return new Intl.DateTimeFormat(text.locale, {
    day: "numeric", month: "long", hour: "2-digit", minute: "2-digit", timeZone: text.timeZone,
  }).format(new Date(value));
}

function updatedLabel(value: string): string {
  return new Intl.DateTimeFormat(text.locale, {
    day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", timeZone: text.timeZone,
  }).format(new Date(value));
}

function sourceLabel(source: string): string {
  return text.sourceNames[source] || source;
}

export default function App() {
  const [section, setSection] = useState<Section>("concerts");
  const [city, setCity] = useState<string>(text.cities[0].value);
  const [date, setDate] = useState("");
  const [search, setSearch] = useState("");
  const [concerts, setConcerts] = useState<Concert[]>([]);
  const [artists, setArtists] = useState<Artist[]>([]);
  const [subscriptions, setSubscriptions] = useState<Artist[]>([]);
  const [favorites, setFavorites] = useState<Concert[]>([]);
  const [profile, setProfile] = useState<Profile | null>(null);
  const [csrf, setCsrf] = useState("");
  const [webLogin, setWebLogin] = useState<WebLoginConfig | null>(null);
  const [webLoginChecked, setWebLoginChecked] = useState(false);
  const [authNotice, setAuthNotice] = useState("");
  const [selected, setSelected] = useState<Concert | null>(null);
  const [selectedArtist, setSelectedArtist] = useState<ArtistDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [page, setPage] = useState(0);
  const [total, setTotal] = useState(0);

  useEffect(() => {
    window.Telegram?.WebApp?.ready();
    window.Telegram?.WebApp?.expand();
    const url = new URL(window.location.href);
    const outcome = url.searchParams.get("auth");
    if (outcome) {
      setAuthNotice(outcome === "cancelled" ? text.errors.webLoginCancelled : outcome === "unavailable" ? text.errors.webLoginUnavailable : text.errors.webLoginFailed);
      url.searchParams.delete("auth");
      window.history.replaceState(null, "", url);
    }
    if (initData) {
      request<Profile>("/auth/telegram", { method: "POST", body: JSON.stringify({ init_data: initData }) })
        .then((signedIn) => { setProfile(signedIn); if (signedIn.city) setCity(signedIn.city); })
        .catch((cause) => setError(cause.message));
      return;
    }
    request<WebLoginConfig>("/auth/web/config")
      .then(setWebLogin)
      .catch(() => setWebLogin(null))
      .finally(() => setWebLoginChecked(true));
    request<WebSession>("/auth/web/session")
      .then((session) => { setProfile(session.profile); setCsrf(session.csrf_token); if (session.profile.city) setCity(session.profile.city); })
      .catch((cause) => { if (!(cause instanceof ApiError && cause.status === 401)) setError(cause.message); });
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
    if (!profile) { setError(text.errors.favoritesSignIn); return; }
    const saved = favorites.some((favorite) => favorite.id === concert.id);
    try {
      await request(`/me/favorites/${concert.id}`, { method: saved ? "DELETE" : "PUT" }, csrf);
      setFavorites(saved ? favorites.filter((favorite) => favorite.id !== concert.id) : [...favorites, concert]);
    } catch (cause) { setError((cause as Error).message); }
  }

  async function toggleSubscription(artist: Artist) {
    if (!profile) { setError(text.errors.subscriptionsSignIn); return; }
    const subscribed = subscriptions.some((entry) => entry.id === artist.id);
    try {
      await request(`/me/subscriptions/${artist.id}`, { method: subscribed ? "DELETE" : "PUT" }, csrf);
      setSubscriptions(subscribed ? subscriptions.filter((entry) => entry.id !== artist.id) : [...subscriptions, artist]);
    } catch (cause) { setError((cause as Error).message); }
  }

  async function saveSettings(changes: Partial<Profile>) {
    if (!profile) return;
    try {
      const updated = await request<Profile>("/me", { method: "PATCH", body: JSON.stringify(changes) }, csrf);
      setProfile(updated);
      if (updated.city) setCity(updated.city);
    } catch (cause) { setError((cause as Error).message); }
  }

  async function signOut() {
    try {
      await request("/auth/web/logout", { method: "POST" }, csrf);
      setProfile(null);
      setCsrf("");
      setSubscriptions([]);
      setFavorites([]);
    } catch (cause) { setError((cause as Error).message); }
  }

  function signInPrompt(message: string) {
    const loginUrl = webLogin?.enabled ? webLogin.login_url : null;
    return <div className="state auth-prompt"><p>{message}</p>
      {!initData && loginUrl && <button className="primary-button" onClick={() => window.location.assign(loginUrl)}>{text.actions.webSignIn}</button>}
      {!initData && !webLoginChecked && <small>{text.states.webLoginChecking}</small>}
      {!initData && webLoginChecked && !webLogin?.enabled && <small>{text.states.webLoginUnavailable}</small>}
    </div>;
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
      <button className="card-main" onClick={() => showConcert(concert)} aria-label={text.format.openConcert(concert.title)}>
        <span className="card-date">{dateLabel(concert.starts_at)}</span>
        <strong>{concert.title}</strong>
        <span className="card-meta">{concert.venue || concert.city}</span>
        {concert.artists.length > 0 && <span className="artist-line">{concert.artists.map((artist) => artist.name).join(" · ")}</span>}
      </button>
      <button className={`save-button ${favorites.some((entry) => entry.id === concert.id) ? "saved" : ""}`} onClick={() => toggleFavorite(concert)} aria-label={text.actions.saveFavorite}>{text.icons.favorites}</button>
    </article>;
  }

  return <div className="app-shell">
    <header className="topbar">
      <button className="brand" onClick={() => { setSection("concerts"); setSelected(null); setSelectedArtist(null); }}><span className="brand-icon">{text.icons.brand}</span> {text.brand}</button>
      <span className="topbar-caption">{text.tagline}</span>
    </header>

    <main>
      {authNotice && <div className="error-banner" role="alert">{authNotice}<button onClick={() => setAuthNotice("")} aria-label={text.actions.closeError}>{text.icons.close}</button></div>}
      {error && <div className="error-banner" role="alert">{error}<button onClick={() => setError("")} aria-label={text.actions.closeError}>{text.icons.close}</button></div>}
      {selected ? <section className="detail-page">
        <button className="back-button" onClick={() => setSelected(null)}>{text.actions.back}</button>
        <div className="eyebrow">{selected.city} · {dateLabel(selected.starts_at)}</div>
        <h1>{selected.title}</h1>
        <p className="detail-venue">{selected.venue || text.placeholders.venue}</p>
        {selected.status === "postponed" && <div className="notice">{text.notices.postponed}</div>}
        {selected.status === "cancelled" && <div className="notice">{text.notices.cancelled}</div>}
        <div className="detail-actions"><button className="primary-button" onClick={() => toggleFavorite(selected)}>{favorites.some((entry) => entry.id === selected.id) ? text.actions.favorited : text.actions.favorite}</button></div>
        {selected.artists.length > 0 && <div className="detail-block"><h2>{text.labels.performers}</h2>{selected.artists.map((artist) => <button className="artist-pill" key={artist.id} onClick={() => showArtist(artist)}>{artist.name} {text.icons.arrow}</button>)}</div>}
        <div className="detail-block"><h2>{text.labels.sources}</h2>{selected.sources.map((source) => <p key={`${source.source}:${source.external_id}`}>{source.url?.startsWith("https://") ? <a href={source.url} target="_blank" rel="noopener noreferrer">{sourceLabel(source.source)} {text.icons.external}</a> : <span>{sourceLabel(source.source)}</span>}<small>{text.format.updatedAt(updatedLabel(source.last_seen_at))}</small></p>)}</div>
      </section> : selectedArtist ? <section className="detail-page">
        <button className="back-button" onClick={() => setSelectedArtist(null)}>{text.actions.back}</button>
        <div className="eyebrow">{text.labels.performer}</div>
        <h1>{selectedArtist.name}</h1>
        {selectedArtist.aliases.length > 0 && <p className="detail-venue">{text.labels.also} {selectedArtist.aliases.join(", ")}</p>}
        <div className="detail-actions"><button className="primary-button" onClick={() => toggleSubscription(selectedArtist)}>{subscriptions.some((entry) => entry.id === selectedArtist.id) ? text.actions.subscribedDetail : text.actions.subscribe}</button></div>
        <div className="detail-block"><h2>{text.labels.upcomingConcerts}</h2>{selectedArtist.concerts.length ? <div className="concert-list">{selectedArtist.concerts.map(concertCard)}</div> : <p className="state">{text.states.noUpcomingConcerts}</p>}</div>
      </section> : <>
        <div className="page-heading"><div className="eyebrow">{text.heading}</div><h1>{text.sections[section]}</h1></div>

        {section === "concerts" && <section>
          <div className="filters"><label>{text.labels.city}<select value={city} onChange={(event) => { setCity(event.target.value); setPage(0); }}>{text.cities.map((cityOption) => <option key={cityOption.value} value={cityOption.value}>{cityOption.label}</option>)}</select></label><label>{text.labels.dateFrom}<input type="date" value={date} onChange={(event) => { setDate(event.target.value); setPage(0); }} /></label></div>
          <label className="search-label"><span>{text.labels.search}</span><input placeholder={text.placeholders.concertSearch} value={search} onChange={(event) => { setSearch(event.target.value); setPage(0); }} /></label>
          {loading ? <p className="state">{text.states.concertsLoading}</p> : concerts.length ? <><div className="concert-list">{concerts.map(concertCard)}</div><div className="pagination"><button disabled={page === 0} onClick={() => setPage(page - 1)}>{text.actions.previous}</button><span>{text.format.pageRange(page * 20 + 1, Math.min((page + 1) * 20, total), total)}</span><button disabled={(page + 1) * 20 >= total} onClick={() => setPage(page + 1)}>{text.actions.next}</button></div></> : <p className="state">{text.states.noConcerts}</p>}
        </section>}

        {section === "artists" && <section><label className="search-label"><span>{text.labels.artistSearch}</span><input placeholder={text.placeholders.artistSearch} value={search} onChange={(event) => setSearch(event.target.value)} /></label>{loading ? <p className="state">{text.states.artistsLoading}</p> : artists.length ? <div className="artist-list">{artists.map((artist) => <div className="artist-row" key={artist.id}><button className="artist-name" onClick={() => showArtist(artist)}>{artist.name}</button><button onClick={() => toggleSubscription(artist)}>{subscriptions.some((entry) => entry.id === artist.id) ? text.actions.subscribed : text.actions.subscribe}</button></div>)}</div> : <p className="state">{text.states.noArtists}</p>}</section>}

        {section === "subscriptions" && <section>{!profile ? signInPrompt(text.states.subscriptionsSignIn) : subscriptions.length ? <div className="artist-list">{subscriptions.map((artist) => <div className="artist-row" key={artist.id}><button className="artist-name" onClick={() => showArtist(artist)}>{artist.name}</button><button onClick={() => toggleSubscription(artist)}>{text.actions.unsubscribe}</button></div>)}</div> : <p className="state">{text.states.noSubscriptions}</p>}</section>}

        {section === "favorites" && <section>{!profile ? signInPrompt(text.states.favoritesSignIn) : favorites.length ? <div className="concert-list">{favorites.map(concertCard)}</div> : <p className="state">{text.states.noFavorites}</p>}</section>}

        {section === "settings" && <section className="settings-panel">{!profile ? signInPrompt(text.states.settingsSignIn) : <><p className="profile-name">{profile.display_name}</p><label>{text.labels.myCity}<select value={profile.city || city} onChange={(event) => saveSettings({ city: event.target.value })}>{text.cities.map((cityOption) => <option key={cityOption.value} value={cityOption.value}>{cityOption.label}</option>)}</select></label><label className="switch-row"><span>{text.labels.notifications}</span><input type="checkbox" checked={profile.notifications_enabled} onChange={(event) => saveSettings({ notifications_enabled: event.target.checked })} /></label>{!initData && csrf && <button className="sign-out-button" onClick={signOut}>{text.actions.webSignOut}</button>}</>}</section>}
      </>}
    </main>

    <nav className="bottom-nav" aria-label={text.labels.sections}>
      {(["concerts", "artists", "subscriptions", "favorites", "settings"] as Section[]).map((entry) => <button key={entry} className={section === entry && !selected && !selectedArtist ? "active" : ""} onClick={() => { setSelected(null); setSelectedArtist(null); setSection(entry); setError(""); }}><span>{text.icons[entry]}</span>{text.sections[entry]}</button>)}
    </nav>
  </div>;
}
