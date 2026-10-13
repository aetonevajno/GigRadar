import en from "../../locales/en.json";
import ru from "../../locales/ru.json";

function webText(translations: typeof ru.web) {
  return {
    ...translations,
    errors: {
      ...translations.errors,
      http: (status: number) => translations.errors.http.replace("{status}", String(status)),
    },
    format: {
      openConcert: (title: string) => translations.format.openConcert.replace("{title}", title),
      updatedAt: (date: string) => translations.format.updatedAt.replace("{date}", date),
      pageRange: (first: number, last: number, total: number) => translations.format.pageRange
        .replace("{first}", String(first))
        .replace("{last}", String(last))
        .replace("{total}", String(total)),
    },
  };
}

export const localization = {
  ru: { label: ru.label, dateLocale: ru.dateLocale, text: webText(ru.web) },
  en: { label: en.label, dateLocale: en.dateLocale, text: webText(en.web) },
};

export type Locale = keyof typeof localization;
export const timeZone = "Europe/Moscow";
export const icons = {
  brand: "✺",
  concerts: "⌁",
  artists: "♫",
  subscriptions: "◎",
  favorites: "☆",
  settings: "⚙",
  close: "×",
  arrow: "→",
  external: "↗",
};
