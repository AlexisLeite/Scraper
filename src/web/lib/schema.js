export function upgradeMoodSchema(raw) {
  let schema;
  try { schema = JSON.parse(raw); }
  catch { return raw; }
  const questions = schema?.questions;
  if (!questions || Object.keys(questions).sort().join(',') !== 'emocion,intensidad,ironia,valencia' ||
      questions.valencia?.type !== 'score' || questions.emocion?.type !== 'choice' ||
      questions.intensidad?.type !== 'score' || questions.ironia?.type !== 'noul' ||
      !Array.isArray(questions.valencia.criteria) ||
      questions.valencia.criteria.join('|') !==
        'Muy negativa|Negativa|Levemente negativa|Neutra o indeterminada|Levemente positiva|Positiva|Muy positiva' ||
      !Array.isArray(questions.intensidad.criteria) ||
      questions.intensidad.criteria.join('|') !== 'Ausente|Sutil|Moderada|Fuerte|Extrema' ||
      Object.keys(questions.emocion.criteria || {}).sort().join(',') !==
        'Alegría,Ansiedad,Desprecio,Enojo,Esperanza,Gratitud,Miedo,Mixta,Neutra,Tristeza') return raw;
  return JSON.stringify({questions: {
    emocion: {
      type: 'choice',
      instructions: 'Identifica la emoción predominante en el mensaje. Si aparecen varias, elige la más intensa; si no hay señales suficientes, elige «indeterminada».',
      criteria: {
        alegría: 'Expresa felicidad, entusiasmo o satisfacción.',
        tristeza: 'Expresa pena, desánimo o pérdida.',
        enojo: 'Expresa ira, irritación o frustración.',
        miedo: 'Expresa temor o sensación de amenaza.',
        ansiedad: 'Expresa preocupación, nerviosismo o incertidumbre.',
        sorpresa: 'Expresa asombro ante algo inesperado.',
        calma: 'Expresa tranquilidad o serenidad.',
        indeterminada: 'No hay señales suficientes para identificar una emoción predominante.'
      }
    },
    intensidad: {
      type: 'score',
      instructions: 'Valora la intensidad de la emoción predominante según el lenguaje del mensaje.',
      criteria: ['Nula o no identificable', 'Leve', 'Moderada', 'Intensa', 'Muy intensa']
    }
  }}, null, 2);
}
