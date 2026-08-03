const THEME_INIT = `(function(){try{var t=localStorage.getItem('fp-theme');var dark=t?t==='dark':true;document.documentElement.classList.toggle('dark',dark);}catch(e){}})()`;

export const themeScript = THEME_INIT;