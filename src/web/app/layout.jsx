import './globals.css';
export const metadata = { title: 'Scrapper · Centro de operaciones', description: 'Configuración y monitor del motor persistente de extracción web.' };
const themeScript = `(function(){var theme;try{theme=localStorage.getItem('scrapper-theme')}catch(e){}document.documentElement.dataset.theme=theme==='dark'||(theme!=='light'&&matchMedia('(prefers-color-scheme: dark)').matches)?'dark':'light'})()`;
export default function Layout({ children }) {
  return <html lang="es" suppressHydrationWarning><head><script dangerouslySetInnerHTML={{__html:themeScript}}/></head><body>{children}</body></html>;
}
