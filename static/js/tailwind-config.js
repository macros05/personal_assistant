/* Shared Tailwind CDN config — single source of truth for the dark palette.
   Loaded by index.html and login.html immediately after the Tailwind CDN script.
   Hex values mirror the CSS variables in /static/css/main.css :root — keep in sync. */
window.tailwind = window.tailwind || {};
tailwind.config = {
  theme: {
    extend: {
      colors: {
        'c-bg':   '#0d0f14',
        'c-s1':   '#13161e',
        'c-s2':   '#1b1f2a',
        'c-bdr':  '#252934',
        'c-acc':  '#5b8dee',
        'c-accd': '#3a5fba',
        'c-txt':  '#e8eaf0',
        'c-dim':  '#8a8fa8',
        'c-mut':  '#555b72',
        'c-usr':  '#1e2a45',
        'c-red':  '#e05a6a',
        'c-grn':  '#4caf82',
        'c-amb':  '#f0a500',
        'c-vio':  '#8b5cf6',
      },
      fontFamily: {
        sans: ['Inter', 'system-ui', '-apple-system', 'sans-serif'],
      },
      width:    { sidebar: '284px' },
      minWidth: { sidebar: '284px' },
      keyframes: {
        cardIn: {
          from: { opacity: '0', transform: 'translateY(16px) scale(.98)' },
          to:   { opacity: '1', transform: 'translateY(0) scale(1)' },
        },
        shake: {
          '0%,100%': { transform: 'translateX(0)' },
          '20%':     { transform: 'translateX(-6px)' },
          '40%':     { transform: 'translateX(6px)' },
          '60%':     { transform: 'translateX(-4px)' },
          '80%':     { transform: 'translateX(4px)' },
        },
      },
      animation: {
        cardIn: 'cardIn .3s cubic-bezier(0.4,0,0.2,1)',
        shake:  'shake .35s ease',
      },
    }
  }
};
