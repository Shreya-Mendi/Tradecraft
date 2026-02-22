import js from '@eslint/js';

export default [
  js.configs.recommended,
  {
    files: ['src/**/*.js'],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: 'module',
      globals: {
        window:          'readonly',
        document:        'readonly',
        localStorage:    'readonly',
        sessionStorage:  'readonly',
        fetch:           'readonly',
        console:         'readonly',
        setTimeout:      'readonly',
        clearTimeout:    'readonly',
        Promise:         'readonly',
        Date:            'readonly',
        JSON:            'readonly',
        Math:            'readonly',
        parseInt:        'readonly',
        parseFloat:      'readonly',
        Boolean:         'readonly',
        String:          'readonly',
        Object:          'readonly',
      },
    },
    rules: {
      'no-unused-vars':   ['warn', { argsIgnorePattern: '^_' }],
      'no-console':       'off',
      'prefer-const':     'error',
      'no-var':           'error',
    },
  },
];
