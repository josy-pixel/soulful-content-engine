/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    extend: {
      colors: {
        brand: { DEFAULT: '#7c3aed', soft: '#ede9fe' },
      },
    },
  },
  plugins: [],
}
