# Comparación del consumo de CI

Objetivo: comparar la matriz paralela actual con una alternativa que ejecute
los servicios seleccionados en un solo trabajo, antes de elegir entre ambas.
La segunda alternativa se preparará para la prueba; este plan no la implementa.

1. Preparar dos versiones equivalentes del workflow: matriz paralela y un
   trabajo único que ejecute los mismos comandos de Gradle para todos los
   servicios seleccionados. Mantener las mismas comprobaciones y versiones.
2. Probar cada versión con el mismo commit y conjuntos de uno, dos y tres
   servicios, además de un cambio compartido que active `ALL-SERVICES`.
3. Repetir cada escenario al menos tres veces con caché caliente y registrar
   por separado una ejecución con caché fría. No comparar ejecuciones con
   diferentes estados de caché como si fueran equivalentes.
4. Registrar para cada ejecución el enlace, los servicios, el resultado, el
   tiempo transcurrido hasta el resultado final y la suma de minutos de todos
   los trabajos, incluida la detección. Anotar los minutos facturados que
   muestre GitHub y el tipo de runner, cuando estén disponibles.
5. Confirmar que ambas alternativas detectan y reportan el mismo fallo, que
   conservan los reportes y que la comprobación final bloquea la fusión.
   Comparar la mediana de duración y de minutos por escenario; decidir con
   el equipo según el consumo y el tiempo de espera observados.

La comparación requiere ejecutar ambas variantes en GitHub Actions. Las
condiciones del runner, los límites del plan y los redondeos de facturación
pueden afectar los resultados; no se presupone cuál será más barata.
