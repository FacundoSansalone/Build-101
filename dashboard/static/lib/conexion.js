// Conexión del panel con el proxy: cuándo darlo por caído y sondeos sin
// superposición. Sin DOM: se testea con node --test.
//
// En el ensayo real, un solo sondeo fallido (o lento) marcaba "proxy
// desconectado" y deshabilitaba el interruptor hasta el próximo sondeo: el
// control parpadeaba y no se podía clickear.

/** Sondeos fallidos seguidos para dar el proxy por desconectado. */
export const FALLOS_PARA_DESCONECTAR = 3;

/**
 * Nuevo estado de la conexión después de un sondeo. Un OK conecta y pone
 * los fallos en 0; un fallo solo desconecta al llegar a FALLOS_PARA_DESCONECTAR
 * seguidos (hasta ahí queda lo que había: conectado o "consultando"). No muta.
 */
export function registrarSondeo(proxy, ok, umbral = FALLOS_PARA_DESCONECTAR) {
  if (ok) return { ...proxy, conectado: true, fallos: 0 };
  const fallos = (proxy?.fallos ?? 0) + 1;
  return { ...proxy, fallos, conectado: fallos >= umbral ? false : (proxy?.conectado ?? null) };
}

/**
 * El interruptor se deshabilita solo si no hay proxy. Mientras cambia no se
 * deshabilita (un control que se apaga y prende bajo el puntero no se puede
 * clickear): el manejador ignora los clics mientras hay un POST en curso.
 */
export function interruptorHabilitado(proxy) {
  return proxy?.conectado === true;
}

/**
 * Envuelve una función asíncrona para que nunca haya dos llamadas en vuelo:
 * si ya hay una, la nueva no hace nada (devuelve undefined). Al terminar,
 * bien o con error, se libera.
 */
export function unaALaVez(fn) {
  let enVuelo = false;
  return async (...args) => {
    if (enVuelo) return undefined;
    enVuelo = true;
    try {
      return await fn(...args);
    } finally {
      enVuelo = false;
    }
  };
}
